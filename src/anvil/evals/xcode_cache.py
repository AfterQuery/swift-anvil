from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import typer
from ruamel.yaml import YAML as _YAML

from ..config import repo_root, source_tasks_dir

from .constants import (
    DEFAULT_BUILD_TIMEOUT,
    PROJECT_PBXPROJ,
    XCODE_CONFIG_APP_TEST_FILES_DEST,
    XCODE_CONFIG_APP_TEST_SCHEME,
    XCODE_CONFIG_APP_TEST_TARGET,
    XCODE_CONFIG_PROJECT,
    XCODE_CONFIG_SCHEME,
    XCODE_CONFIG_TEST_FILES_DEST,
    XCODE_CONFIG_TEST_SCHEME,
    XCODE_CONFIG_UI_TEST_FILES_DEST,
    XCODE_CONFIG_UI_TEST_TARGET,
)

logger = logging.getLogger(__name__)

# DerivedData subdirectory names inside each commit cache dir / worktree.
_DD_DIR = "DerivedData"
_TEST_DD_DIR = "DerivedData-tests"
_APP_TEST_DD_DIR = "DerivedData-app-tests"

# Warmup Swift file dropped into the test target during cache warming.
_WARMUP_FILENAME = "_anvil_warmup.swift"

# Standard Xcode pbxproj constants.
_PBX_UUID_LENGTH = 24
_PBX_BUILD_ACTION_MASK = 2147483647  # INT32_MAX — Xcode default for all build phases


def _pbx_uuid(seed: str) -> str:
    """Deterministic 24-char uppercase hex UUID for pbxproj entries."""
    return hashlib.md5(seed.encode()).hexdigest().upper()[:_PBX_UUID_LENGTH]


def _apfs_clone(src: Path, dst: Path) -> None:
    """Copy a directory tree using APFS clonefile (instant COW on macOS).

    Falls back to :func:`shutil.copytree` on non-macOS or non-APFS volumes.
    """
    if sys.platform == "darwin":
        result = subprocess.run(
            ["cp", "-c", "-r", "-p", str(src), str(dst)],
            capture_output=True,
        )
        if result.returncode == 0:
            return
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        logger.debug("cp -c failed, falling back to shutil.copytree: %s", result.stderr)
    shutil.copytree(str(src), str(dst), symlinks=True)


def _dd_is_populated(path: Path) -> bool:
    """Return True if *path* exists and contains at least one entry."""
    return path.exists() and any(path.iterdir())


def _clone_dd_if_populated(src: Path, dst: Path) -> None:
    """APFS-clone a DerivedData directory if it has content."""
    if _dd_is_populated(src):
        _apfs_clone(src, dst)


def _remove_worktree(clone_dir: Path, work_dir: Path) -> None:
    """Remove a git worktree, falling back to rmtree if git fails."""
    _run_cmd(
        ["git", "-C", str(clone_dir), "worktree", "remove", "--force", str(work_dir)],
        check=False,
    )
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)


def _format_build_errors(
    stderr: str,
    stdout: str = "",
    max_lines: int = 10,
    fallback_chars: int = 1200,
) -> str:
    """Extract useful xcodebuild failure lines, with a combined-stream fallback."""
    combined = "\n".join(part for part in [stderr, stdout] if part)
    error_lines = [ln for ln in combined.splitlines() if "error:" in ln.lower()]
    if error_lines:
        return "\n".join(error_lines[:max_lines])
    if "The following build commands failed:" in combined:
        tail = combined.split("The following build commands failed:")[-1]
        return "The following build commands failed:" + tail[:fallback_chars]
    return combined[-fallback_chars:]


def _is_package_resolution_failure(stdout: str, stderr: str) -> bool:
    """Return True when xcodebuild failed during SwiftPM dependency resolution."""
    needle = "could not resolve package dependencies"
    return needle in stdout.lower() or needle in stderr.lower()


def _default_cache_root() -> Path:
    return repo_root() / ".xcode-cache"


def _build_timeout(xcode_config: dict) -> int:
    raw = xcode_config.get("build_timeout", DEFAULT_BUILD_TIMEOUT)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_BUILD_TIMEOUT


def _run_pre_build_commands(xcode_config: dict, work_dir: Path) -> None:
    """Run any pre_build_commands listed in xcode_config before building."""
    cmds = xcode_config.get("pre_build_commands", [])
    if not cmds:
        return
    for cmd in cmds:
        logger.info("Running pre-build command: %s", cmd)
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(
                "Pre-build command failed (continuing): %s\nstderr: %s",
                cmd,
                result.stderr[-500:],
            )


# Xcode 15+ dropped libarclite (needed for iOS < 12) and Swift 4 support.
_MIN_IPHONEOS_DEPLOYMENT_TARGET = "12.0"
_MIN_SWIFT_VERSION = "5.0"

_RENAME_FIXIT_MAX_PASSES = 5


def _bump_pbxproj_min_ios(pbx_path: Path, min_ios: str) -> None:
    """Raise every IPHONEOS_DEPLOYMENT_TARGET in *pbx_path* below *min_ios*."""
    if not pbx_path.exists():
        return
    pbx = pbx_path.read_text()
    floor = tuple(int(p) for p in min_ios.split("."))

    def bump(m: re.Match[str]) -> str:
        cur = tuple(int(p) for p in m.group(1).split("."))
        return f"IPHONEOS_DEPLOYMENT_TARGET = {min_ios};" if cur < floor else m.group(0)

    new_pbx = re.sub(r"IPHONEOS_DEPLOYMENT_TARGET = (\d+(?:\.\d+)*);", bump, pbx)
    if new_pbx != pbx:
        _write_patched_source(pbx_path, new_pbx)
        logger.info("Bumped IPHONEOS_DEPLOYMENT_TARGET to %s in %s", min_ios, pbx_path)


def _bump_main_project_min_ios(
    xcode_config: dict, work_dir: Path, min_ios: str
) -> None:
    """Bump the main Xcode project's deployment target floor to *min_ios*."""
    project_rel, _ = resolve_repo_relative_path(
        xcode_config.get(XCODE_CONFIG_PROJECT, ""), work_dir
    )
    if not project_rel:
        return
    _bump_pbxproj_min_ios(work_dir / project_rel / PROJECT_PBXPROJ, min_ios)


def _patch_podfile_build_settings(work_dir: Path, min_ios: str, min_swift: str) -> None:
    """Inject a Podfile post_install hook bumping pod deployment / Swift floors."""
    podfile = work_dir / "Podfile"
    if not podfile.exists():
        return
    content = podfile.read_text()
    marker = "# anvil-pod-compat-workaround"
    if marker in content:
        return

    bump_block = (
        f"  {marker}\n"
        f"  installer.pods_project.targets.each do |target|\n"
        f"    target.build_configurations.each do |config|\n"
        f"      ios_target = config.build_settings['IPHONEOS_DEPLOYMENT_TARGET']\n"
        f"      if ios_target.nil? || Gem::Version.new(ios_target) < Gem::Version.new('{min_ios}')\n"
        f"        config.build_settings['IPHONEOS_DEPLOYMENT_TARGET'] = '{min_ios}'\n"
        f"      end\n"
        f"      swift_version = config.build_settings['SWIFT_VERSION']\n"
        f"      if swift_version && Gem::Version.new(swift_version) < Gem::Version.new('{min_swift}')\n"
        f"        config.build_settings['SWIFT_VERSION'] = '{min_swift}'\n"
        f"      end\n"
        f"    end\n"
        f"  end\n"
    )

    pattern = re.compile(r"(post_install\s+do\s*\|[^|]+\|\s*\n)")
    if pattern.search(content):
        new_content = pattern.sub(r"\1" + bump_block, content, count=1)
    else:
        new_content = (
            content.rstrip()
            + "\n\npost_install do |installer|\n"
            + bump_block
            + "end\n"
        )

    podfile.write_text(new_content)
    logger.info(
        "Patched Podfile: IPHONEOS_DEPLOYMENT_TARGET>=%s, SWIFT_VERSION>=%s",
        min_ios,
        min_swift,
    )


# File-scoped Swift 5 fix-its for pinned pod versions.
_POD_SOURCE_PATCHES: tuple[tuple[str, "re.Pattern[str]", str], ...] = (
    # `@available(*, unavailable)` on a stored property is a hard error in Swift 5+.
    (
        "Pods/Pageboy/Sources/Pageboy/PageboyViewController.swift",
        re.compile(
            r"^[ \t]*@available\(\*, unavailable,[^)]*\)\n"
            r"(?=[ \t]*(?:public|open|internal|private|fileprivate)?\s*"
            r"(?:var|let)\s+showsPageControl\b)",
            re.MULTILINE,
        ),
        "",
    ),
    # kCATransition* constants are CATransitionSubtype in Swift 5, not String.
    (
        "Pods/Pageboy/Sources/Pageboy/Utilities/Transitioning/TransitionOperation+Action.swift",
        re.compile(r"(\bvar\s+transitionSubType\s*:\s*)String(\s*\{)"),
        r"\1CATransitionSubtype\2",
    ),
    # CATransition.type became CATransitionType.
    (
        "Pods/Pageboy/Sources/Pageboy/Extensions/PageboyViewController+Transitioning.swift",
        re.compile(r"(\btransition\.type\s*=\s*)(self\.style\.rawValue)\b"),
        r"\1CATransitionType(rawValue: \2)",
    ),
    # UIPageViewController option keys are now UIPageViewController.OptionsKey.
    (
        "Pods/Pageboy/Sources/Pageboy/Extensions/PageboyViewController+Management.swift",
        re.compile(r"\[String\s*:\s*Any\]"),
        "[UIPageViewController.OptionsKey: Any]",
    ),
    # `open` member in a `final` class — fix-it suggests `public`.
    (
        "Pods/MessageViewController/MessageViewController/MessageAutocompleteController.swift",
        re.compile(r"\bopen\s+var\s+appendSpaceOnCompletion\b"),
        "public var appendSpaceOnCompletion",
    ),
    # `preserveTypingAttributes` builds a local [String: Any] then assigns to
    # textView.typingAttributes which became [NSAttributedString.Key: Any].
    (
        "Pods/MessageViewController/MessageViewController/MessageAutocompleteController.swift",
        re.compile(r"(\bvar\s+typingAttributes\s*=\s*)\[String\s*:\s*Any\]\(\)"),
        r"\1[NSAttributedString.Key: Any]()",
    ),
    (
        "Pods/MessageViewController/MessageViewController/MessageAutocompleteController.swift",
        re.compile(r"typingAttributes\[\$0\.key\.rawValue\]"),
        "typingAttributes[$0.key]",
    ),
    # UITextView.typingAttributes is [NSAttributedString.Key: Any] in Swift 5.
    (
        "Pods/MessageViewController/MessageViewController/MessageTextView.swift",
        re.compile(r"(\bdefaultTextAttributes\s*:\s*)\[String\s*:\s*Any\]"),
        r"\1[NSAttributedString.Key: Any]",
    ),
    # Drop now-redundant `.rawValue` so keys match the new dict type.
    (
        "Pods/MessageViewController/MessageViewController/MessageTextView.swift",
        re.compile(r"(NSAttributedString\.Key\.\w+)\.rawValue\b"),
        r"\1",
    ),
    # Old Apollo codegen lists `RawRepresentable, Equatable` without `Hashable`;
    # Swift 5+ no longer auto-synthesises Hashable from RawRepresentable.
    (
        "gql/API.swift",
        re.compile(r"(\benum\s+\w+\s*:\s*RawRepresentable,\s*Equatable,)"),
        r"\1 Hashable,",
    ),
    # Older Apollo codegen used `enum X: String` plain.  The auto-Hashable
    # synthesis can fail when an extension later adds Apollo protocols, so
    # declare it explicitly.
    (
        "gql/API.swift",
        re.compile(r"(\benum\s+\w+\s*:\s*String)\s*\{"),
        r"\1, Hashable {",
    ),
    # Apollo's playground display extension uses unavailable Swift 4 protocol.
    (
        "Pods/Apollo/Sources/Apollo/RecordSet.swift",
        re.compile(
            r"extension RecordSet:\s*CustomPlaygroundQuickLookable\s*\{\s*\n"
            r"\s*public var customPlaygroundQuickLook:\s*PlaygroundQuickLook\s*\{\s*\n"
            r"\s*return\s+\.text\(description\)\s*\n"
            r"\s*\}\s*\n"
            r"\}",
        ),
        (
            "extension RecordSet: CustomPlaygroundDisplayConvertible {\n"
            "  public var playgroundDescription: Any {\n"
            "    return description\n"
            "  }\n"
            "}"
        ),
    ),
    # Apollo's GroupedSequenceIterator hard-codes the Swift-4 enumerated-iterator
    # type; type-erase with AnyIterator so it works regardless of Swift mode.
    (
        "Pods/Apollo/Sources/Apollo/Collections.swift",
        re.compile(
            r"private var keyIterator:\s*"
            r"(?:EnumeratedSequence<Array<Key>>\.Iterator"
            r"|EnumeratedIterator<IndexingIterator<Array<Key>>>)"
        ),
        "private var keyIterator: AnyIterator<(offset: Int, element: Key)>",
    ),
    (
        "Pods/Apollo/Sources/Apollo/Collections.swift",
        re.compile(r"keyIterator\s*=\s*base\.keys\.enumerated\(\)\.makeIterator\(\)"),
        "keyIterator = AnyIterator(base.keys.enumerated().makeIterator())",
    ),
)

# Swift 4 → 5 type/constant renames applied across all pod sources.
_POD_SWIFT_RENAMES: tuple[tuple["re.Pattern[str]", str], ...] = tuple(
    (re.compile(rf"\b{old}\b"), new)
    for old, new in (
        # Foundation
        ("NSAttributedStringKey", "NSAttributedString.Key"),
        ("NSLayoutRelation", "NSLayoutConstraint.Relation"),
        ("NSLayoutAttribute", "NSLayoutConstraint.Attribute"),
        ("NSLayoutFormatOptions", "NSLayoutConstraint.FormatOptions"),
        # UIFont / UIFontDescriptor
        ("UIFontDescriptorSymbolicTraits", "UIFontDescriptor.SymbolicTraits"),
        ("UIFontWeight", "UIFont.Weight"),
        # UIView
        ("UIViewContentMode", "UIView.ContentMode"),
        ("UIViewAnimationOptions", "UIView.AnimationOptions"),
        ("UIViewAnimationCurve", "UIView.AnimationCurve"),
        ("UIViewAutoresizing", "UIView.AutoresizingMask"),
        # UIControl
        ("UIControlState", "UIControl.State"),
        ("UIControlEvents", "UIControl.Event"),
        ("UIControlContentHorizontalAlignment", "UIControl.ContentHorizontalAlignment"),
        ("UIControlContentVerticalAlignment", "UIControl.ContentVerticalAlignment"),
        # UITableView / UITableViewCell
        ("UITableViewRowAnimation", "UITableView.RowAnimation"),
        ("UITableViewScrollPosition", "UITableView.ScrollPosition"),
        ("UITableViewStyle", "UITableView.Style"),
        ("UITableViewCellStyle", "UITableViewCell.CellStyle"),
        ("UITableViewCellAccessoryType", "UITableViewCell.AccessoryType"),
        ("UITableViewCellSelectionStyle", "UITableViewCell.SelectionStyle"),
        ("UITableViewCellSeparatorStyle", "UITableViewCell.SeparatorStyle"),
        ("UITableViewCellEditingStyle", "UITableViewCell.EditingStyle"),
        # UIAlert
        ("UIAlertActionStyle", "UIAlertAction.Style"),
        ("UIAlertControllerStyle", "UIAlertController.Style"),
        # UIPageViewController
        (
            "UIPageViewControllerNavigationOrientation",
            "UIPageViewController.NavigationOrientation",
        ),
        (
            "UIPageViewControllerNavigationDirection",
            "UIPageViewController.NavigationDirection",
        ),
        (
            "UIPageViewControllerTransitionStyle",
            "UIPageViewController.TransitionStyle",
        ),
        (
            "UIPageViewControllerSpineLocation",
            "UIPageViewController.SpineLocation",
        ),
        # UIButton / UIBarButtonItem
        ("UIButtonType", "UIButton.ButtonType"),
        ("UIBarButtonSystemItem", "UIBarButtonItem.SystemItem"),
        ("UIBarButtonItemStyle", "UIBarButtonItem.Style"),
        # UIImage
        ("UIImageOrientation", "UIImage.Orientation"),
        ("UIImageRenderingMode", "UIImage.RenderingMode"),
        ("UIImageResizingMode", "UIImage.ResizingMode"),
        # Misc UIKit
        ("UIActivityIndicatorViewStyle", "UIActivityIndicatorView.Style"),
        ("UIScrollViewKeyboardDismissMode", "UIScrollView.KeyboardDismissMode"),
        ("UITextFieldViewMode", "UITextField.ViewMode"),
        ("UITextBorderStyle", "UITextField.BorderStyle"),
        ("UIBlurEffectStyle", "UIBlurEffect.Style"),
        ("NSTextStorageEditActions", "NSTextStorage.EditActions"),
        ("UIImpactFeedbackStyle", "UIImpactFeedbackGenerator.FeedbackStyle"),
        # CoreAnimation
        ("kCATransitionFromLeft", "CATransitionSubtype.fromLeft"),
        ("kCATransitionFromRight", "CATransitionSubtype.fromRight"),
        ("kCATransitionFromTop", "CATransitionSubtype.fromTop"),
        ("kCATransitionFromBottom", "CATransitionSubtype.fromBottom"),
        ("kCATransitionFade", "CATransitionType.fade"),
        ("kCATransitionMoveIn", "CATransitionType.moveIn"),
        ("kCATransitionPush", "CATransitionType.push"),
        ("kCATransitionReveal", "CATransitionType.reveal"),
        ("kCAFillModeForwards", "CAMediaTimingFillMode.forwards"),
        ("kCAFillModeBackwards", "CAMediaTimingFillMode.backwards"),
        ("kCAFillModeBoth", "CAMediaTimingFillMode.both"),
        ("kCAFillModeRemoved", "CAMediaTimingFillMode.removed"),
        # UIKeyboard userInfo
        (
            "UIKeyboardFrameBeginUserInfoKey",
            "UIResponder.keyboardFrameBeginUserInfoKey",
        ),
        (
            "UIKeyboardFrameEndUserInfoKey",
            "UIResponder.keyboardFrameEndUserInfoKey",
        ),
        (
            "UIKeyboardAnimationDurationUserInfoKey",
            "UIResponder.keyboardAnimationDurationUserInfoKey",
        ),
        (
            "UIKeyboardAnimationCurveUserInfoKey",
            "UIResponder.keyboardAnimationCurveUserInfoKey",
        ),
        (
            "UIKeyboardIsLocalUserInfoKey",
            "UIResponder.keyboardIsLocalUserInfoKey",
        ),
        # UIPageViewController options
        (
            "UIPageViewControllerOptionInterPageSpacingKey",
            "UIPageViewController.OptionsKey.interPageSpacing",
        ),
        (
            "UIPageViewControllerOptionSpineLocationKey",
            "UIPageViewController.OptionsKey.spineLocation",
        ),
        ("kCAGravityCenter", "CALayerContentsGravity.center"),
        ("kCAGravityTop", "CALayerContentsGravity.top"),
        ("kCAGravityBottom", "CALayerContentsGravity.bottom"),
        ("kCAGravityLeft", "CALayerContentsGravity.left"),
        ("kCAGravityRight", "CALayerContentsGravity.right"),
        ("kCAGravityTopLeft", "CALayerContentsGravity.topLeft"),
        ("kCAGravityTopRight", "CALayerContentsGravity.topRight"),
        ("kCAGravityBottomLeft", "CALayerContentsGravity.bottomLeft"),
        ("kCAGravityBottomRight", "CALayerContentsGravity.bottomRight"),
        ("kCAGravityResize", "CALayerContentsGravity.resize"),
        ("kCAGravityResizeAspect", "CALayerContentsGravity.resizeAspect"),
        ("kCAGravityResizeAspectFill", "CALayerContentsGravity.resizeAspectFill"),
    )
)

# Notification names; the leading `.` or `NSNotification.Name.` prefix is
# absorbed by the pattern so the rewrite drops it.
_POD_SWIFT_NOTIFICATION_RENAMES: tuple[tuple["re.Pattern[str]", str], ...] = tuple(
    (
        re.compile(rf"(?:\bNSNotification\.Name\.|\.){old}\b"),
        new,
    )
    for old, new in (
        # UIApplication
        (
            "UIApplicationDidReceiveMemoryWarning",
            "UIApplication.didReceiveMemoryWarningNotification",
        ),
        (
            "UIApplicationDidEnterBackground",
            "UIApplication.didEnterBackgroundNotification",
        ),
        (
            "UIApplicationWillEnterForeground",
            "UIApplication.willEnterForegroundNotification",
        ),
        ("UIApplicationDidBecomeActive", "UIApplication.didBecomeActiveNotification"),
        ("UIApplicationWillResignActive", "UIApplication.willResignActiveNotification"),
        (
            "UIApplicationDidFinishLaunching",
            "UIApplication.didFinishLaunchingNotification",
        ),
        ("UIApplicationWillTerminate", "UIApplication.willTerminateNotification"),
        (
            "UIApplicationSignificantTimeChange",
            "UIApplication.significantTimeChangeNotification",
        ),
        (
            "UIApplicationBackgroundRefreshStatusDidChange",
            "UIApplication.backgroundRefreshStatusDidChangeNotification",
        ),
        (
            "UIApplicationProtectedDataWillBecomeUnavailable",
            "UIApplication.protectedDataWillBecomeUnavailableNotification",
        ),
        (
            "UIApplicationProtectedDataDidBecomeAvailable",
            "UIApplication.protectedDataDidBecomeAvailableNotification",
        ),
        (
            "UIApplicationUserDidTakeScreenshot",
            "UIApplication.userDidTakeScreenshotNotification",
        ),
        # UIDevice
        (
            "UIDeviceOrientationDidChange",
            "UIDevice.orientationDidChangeNotification",
        ),
        # UIKeyboard
        ("UIKeyboardWillShow", "UIResponder.keyboardWillShowNotification"),
        ("UIKeyboardDidShow", "UIResponder.keyboardDidShowNotification"),
        ("UIKeyboardWillHide", "UIResponder.keyboardWillHideNotification"),
        ("UIKeyboardDidHide", "UIResponder.keyboardDidHideNotification"),
        (
            "UIKeyboardWillChangeFrame",
            "UIResponder.keyboardWillChangeFrameNotification",
        ),
        (
            "UIKeyboardDidChangeFrame",
            "UIResponder.keyboardDidChangeFrameNotification",
        ),
    )
)

# Swift 4→5 renames whose callsites need custom regexes.  Order matters: more
# specific patterns must precede their shorthand counterparts.
_POD_SWIFT_PATTERN_RENAMES: tuple[tuple["re.Pattern[str]", str], ...] = (
    (
        re.compile(
            r"\bUIAccessibilityPostNotification\s*\(\s*"
            r"UIAccessibilityAnnouncementNotification\s*,\s*"
        ),
        "UIAccessibility.post(notification: .announcement, argument: ",
    ),
    (re.compile(r"\bremoveFromParentViewController\s*\(\s*\)"), "removeFromParent()"),
    (re.compile(r"\baddChildViewController\s*\("), "addChild("),
    (
        re.compile(r"\bdidMove\s*\(\s*toParentViewController\s*:"),
        "didMove(toParent:",
    ),
    (re.compile(r"\.sendSubview\s*\(\s*toBack\s*:"), ".sendSubviewToBack("),
    (re.compile(r"\.bringSubview\s*\(\s*toFront\s*:"), ".bringSubviewToFront("),
    (re.compile(r"\bRunLoopMode\.commonModes\b"), "RunLoop.Mode.common"),
    (re.compile(r"\bRunLoopMode\b(?!\.commonModes\b)"), "RunLoop.Mode"),
    (re.compile(r"\.commonModes\b"), ".common"),
    # `let` is implicitly final, so `open let` is never legal Swift.
    (re.compile(r"\bopen(\s+let\b)"), r"public\1"),
    # Same for `static` declarations.
    (re.compile(r"\bopen(\s+static\b)"), r"public\1"),
    # String.characters was removed; String itself is the Collection.
    (re.compile(r"\.characters\b"), ""),
)

# Top-level UIAccessibility C functions → properties; callsite drops the `()`.
_POD_SWIFT_FUNC_TO_PROP: tuple[tuple["re.Pattern[str]", str], ...] = tuple(
    (re.compile(rf"\b{old}\(\)"), new)
    for old, new in (
        ("UIAccessibilityIsVoiceOverRunning", "UIAccessibility.isVoiceOverRunning"),
        (
            "UIAccessibilityIsSwitchControlRunning",
            "UIAccessibility.isSwitchControlRunning",
        ),
        (
            "UIAccessibilityIsAssistiveTouchRunning",
            "UIAccessibility.isAssistiveTouchRunning",
        ),
        ("UIAccessibilityIsMonoAudioEnabled", "UIAccessibility.isMonoAudioEnabled"),
        (
            "UIAccessibilityIsClosedCaptioningEnabled",
            "UIAccessibility.isClosedCaptioningEnabled",
        ),
        (
            "UIAccessibilityIsInvertColorsEnabled",
            "UIAccessibility.isInvertColorsEnabled",
        ),
        (
            "UIAccessibilityIsGuidedAccessEnabled",
            "UIAccessibility.isGuidedAccessEnabled",
        ),
        ("UIAccessibilityIsBoldTextEnabled", "UIAccessibility.isBoldTextEnabled"),
        ("UIAccessibilityIsGrayscaleEnabled", "UIAccessibility.isGrayscaleEnabled"),
        (
            "UIAccessibilityIsReduceTransparencyEnabled",
            "UIAccessibility.isReduceTransparencyEnabled",
        ),
        (
            "UIAccessibilityIsReduceMotionEnabled",
            "UIAccessibility.isReduceMotionEnabled",
        ),
        (
            "UIAccessibilityIsDarkerSystemColorsEnabled",
            "UIAccessibility.isDarkerSystemColorsEnabled",
        ),
        (
            "UIAccessibilityIsSpeakSelectionEnabled",
            "UIAccessibility.isSpeakSelectionEnabled",
        ),
        (
            "UIAccessibilityIsSpeakScreenEnabled",
            "UIAccessibility.isSpeakScreenEnabled",
        ),
        (
            "UIAccessibilityIsShakeToUndoEnabled",
            "UIAccessibility.isShakeToUndoEnabled",
        ),
    )
)

# Pods/ holds remote-spec pods; Local Pods/ holds :path => vendored pods.
_POD_SOURCE_ROOTS: tuple[str, ...] = ("Pods", "Local Pods")


def _write_patched_source(path: Path, content: str) -> None:
    """Write *content*, restoring the original mode if it was read-only (0444 pods)."""
    try:
        path.write_text(content)
    except PermissionError:
        original_mode = path.stat().st_mode
        path.chmod(original_mode | 0o200)
        try:
            path.write_text(content)
        finally:
            path.chmod(original_mode)


def _strip_stray_info_plist_from_pods_project(work_dir: Path) -> None:
    """Remove bogus ``Info.plist in Sources`` entries from Pods.xcodeproj.

    Some podspecs glob their framework ``Info.plist`` into the target's Sources
    phase; xcodebuild then emits two ``ProcessInfoPlistFile`` tasks for the
    same output and aborts with ``error: Unexpected duplicate tasks``.  The
    canonical ``INFOPLIST_FILE`` from Target Support Files is untouched.
    """
    pbx_path = work_dir / "Pods" / "Pods.xcodeproj" / PROJECT_PBXPROJ
    if not pbx_path.exists():
        return
    pbx = pbx_path.read_text()

    # CocoaPods emits 32-char UUIDs, Xcode emits 24-char.
    build_file_line = re.compile(
        r"^\t+(\w{24,32}) /\* Info\.plist in Sources \*/ = \{[^}]*\};\n",
        re.MULTILINE,
    )
    stray_uuids = [m.group(1) for m in build_file_line.finditer(pbx)]
    if not stray_uuids:
        return

    patched = build_file_line.sub("", pbx)
    for uuid in stray_uuids:
        phase_line = re.compile(
            rf"^\t+{uuid} /\* Info\.plist in Sources \*/,\n", re.MULTILINE
        )
        patched = phase_line.sub("", patched)

    if patched != pbx:
        _write_patched_source(pbx_path, patched)
        logger.info(
            "Stripped %d stray Info.plist build-file entries from Pods.xcodeproj",
            len(stray_uuids),
        )


# xcodebuild "X has been renamed/replaced by Y" diagnostics, plus the
# deprecation-as-error variant some build configs emit.
_RENAME_DIAGNOSTIC = re.compile(
    r"^(?P<file>/[^:\n]+\.swift):(?P<line>\d+):(?P<col>\d+):\s*"
    r"error: '(?P<old>[^']+)' "
    r"(?:"
    r"has been renamed to"
    r"|has been replaced by(?: property)?"
    r"|is deprecated:.* use"
    r") "
    r"'(?P<new>[^']+)'",
    re.MULTILINE,
)

# "<decl-kind> are implicitly 'final'; use 'X' instead of 'open'" — column points at `open`.
_IMPLICIT_FINAL_DIAGNOSTIC = re.compile(
    r"^(?P<file>/[^:\n]+\.swift):(?P<line>\d+):(?P<col>\d+):\s*"
    r"error: [^\n]*implicitly 'final'; use '(?P<modifier>\w+)' instead of 'open'",
    re.MULTILINE,
)

# "'X' is unavailable: <hint>" — only known fix-able instance is String.characters.
_UNAVAILABLE_DIAGNOSTIC = re.compile(
    r"^(?P<file>/[^:\n]+\.swift):(?P<line>\d+):(?P<col>\d+):\s*"
    r"error: '(?P<symbol>[^']+)' is unavailable[^\n]*",
    re.MULTILINE,
)

# "binary operator '|=' cannot be applied" — OptionSet types lost their
# bitwise operators in Swift 5+.  Use the equivalent set-mutation method.
_OPSET_OPERATOR_DIAGNOSTIC = re.compile(
    r"^(?P<file>/[^:\n]+\.swift):(?P<line>\d+):(?P<col>\d+):\s*"
    r"error: binary operator '(?P<op>\|=|&=)' cannot be applied",
    re.MULTILINE,
)

_OPSET_OPERATOR_METHOD = {"|=": "formUnion", "&=": "formIntersection"}

# "using '!' is not allowed here" — Swift 5+ rejects implicitly-unwrapped types
# in cast/generic positions.  Column points at the `!`; drop it.
_BANG_NOT_ALLOWED_DIAGNOSTIC = re.compile(
    r"^(?P<file>/[^:\n]+\.swift):(?P<line>\d+):(?P<col>\d+):\s*"
    r"error: using '!' is not allowed here",
    re.MULTILINE,
)

# "switch must be exhaustive" — Swift 5 requires @unknown default for
# non-frozen system enums.  Line points at the `switch` keyword.
_NONEXHAUSTIVE_SWITCH_DIAGNOSTIC = re.compile(
    r"^(?P<file>/[^:\n]+\.swift):(?P<line>\d+):(?P<col>\d+):\s*"
    r"error: switch must be exhaustive",
    re.MULTILINE,
)

# "'X' is not overridable; did you mean to override 'Y'?" — diagnostic names
# the correct member.  Rewrite the old member's last name → new on the cited line.
_NOT_OVERRIDABLE_DIAGNOSTIC = re.compile(
    r"^(?P<file>/[^:\n]+\.swift):(?P<line>\d+):(?P<col>\d+):\s*"
    r"error: '(?P<old>[^']+)' is not overridable; "
    r"did you mean to override '(?P<new>[^']+)'\?",
    re.MULTILINE,
)


def _split_callable_name(name: str) -> tuple[str, str | None]:
    """Split ``foo(bar:)`` → (``foo``, ``bar:``); plain identifier → (name, None)."""
    m = re.match(r"^([\w.]+)\((.*)\)$", name)
    if not m:
        return name, None
    return m.group(1), m.group(2)


def _build_rename_replacements(
    old: str, new: str
) -> list[tuple["re.Pattern[str]", str]]:
    """Build regex rewrites for one rename diagnostic; multiple shapes possible."""
    old_base, old_args = _split_callable_name(old)
    new_base, new_args = _split_callable_name(new)

    # OldFn(a, b) → Type(label1: a, label2: b) for C-style "Make" constructors.
    if old_args is None and new_base.endswith(".init") and new_args:
        type_name = new_base[: -len(".init")]
        labels = [lbl.rstrip(":") for lbl in new_args.split(":") if lbl]
        if labels:
            arg_group = ",".join([r"\s*([^,)]+)"] * len(labels))
            pattern = re.compile(rf"\b{re.escape(old)}\s*\({arg_group}\s*\)")
            replacement = (
                f"{type_name}("
                + ", ".join(f"{lbl}: \\{i + 1}" for i, lbl in enumerate(labels))
                + ")"
            )
            return [(pattern, replacement)]

    # foo() → Bar.foo (function-to-property)
    if old_args == "" and new_args is None:
        return [(re.compile(rf"\b{re.escape(old_base)}\(\)"), new_base)]

    # bringSubview(toFront:) → bringSubviewToFront(_:) — rewrite method head only.
    if old_args is not None and new_args is not None:
        old_method = old_base.rsplit(".", 1)[-1]
        new_method = new_base.rsplit(".", 1)[-1]
        return [
            (
                re.compile(rf"\.{re.escape(old_method)}\s*\(\s*"),
                f".{new_method}(",
            )
        ]

    # Renames whose new name is a nested path (`A.B[.C]`): the old form is
    # often referenced via shorthand member access (`.X`) or a now-renamed
    # parent prefix (`OldType.X`, `NSNotification.Name.X`).  Eat that prefix so
    # the rewrite doesn't leave a stray leading `.` (`.RunLoop.Mode.tracking`).
    if "." in new:
        return [
            (re.compile(rf"(?:\bNSNotification\.Name\.|\.){re.escape(old)}\b"), new),
            (re.compile(rf"\b{re.escape(old)}\b"), new),
        ]

    return [(re.compile(rf"\b{re.escape(old)}\b"), new)]


_OPEN_KEYWORD = re.compile(r"\bopen\b")
_DOT_CHARACTERS = re.compile(r"\.characters\b")


def _replace_on_line(
    content: str, line_no: int, pattern: "re.Pattern[str]", replacement: str
) -> str:
    """Apply *pattern* → *replacement* (count=1) on the 1-based *line_no*."""
    lines = content.split("\n")
    if 1 <= line_no <= len(lines):
        lines[line_no - 1] = pattern.sub(replacement, lines[line_no - 1], count=1)
    return "\n".join(lines)


def _add_unknown_default(content: str, switch_line_no: int) -> str:
    """Insert ``@unknown default: break`` before the close brace of the switch on *switch_line_no*."""
    lines = content.split("\n")
    if not (1 <= switch_line_no <= len(lines)):
        return content
    if "@unknown default" in lines[switch_line_no - 1]:
        return content

    # Walk forward counting braces from the `switch` line until we close the body.
    depth = 0
    seen_open = False
    for idx in range(switch_line_no - 1, len(lines)):
        line = lines[idx]
        for ch in line:
            if ch == "{":
                depth += 1
                seen_open = True
            elif ch == "}":
                depth -= 1
                if seen_open and depth == 0:
                    if any(
                        "@unknown default" in lines[i]
                        for i in range(switch_line_no - 1, idx + 1)
                    ):
                        return content
                    case_indent = re.match(r"[ \t]*", lines[idx]).group(0)
                    body_indent = case_indent + "    "
                    lines[idx:idx] = [
                        f"{case_indent}@unknown default:",
                        f"{body_indent}break",
                    ]
                    return "\n".join(lines)
    return content


def _apply_compiler_fixits(xcodebuild_output: str, work_dir: Path) -> int:
    """Apply rename / implicit-final / unavailable fix-its from a build log."""

    file_state: dict[Path, str] = {}

    def load(raw_path: str) -> tuple[Path, str] | None:
        path = Path(raw_path)
        try:
            path.relative_to(work_dir)
        except ValueError:
            return None
        if path not in file_state:
            if not path.exists():
                return None
            file_state[path] = path.read_text()
        return path, file_state[path]

    for m in _RENAME_DIAGNOSTIC.finditer(xcodebuild_output):
        loaded = load(m.group("file"))
        if loaded is None:
            continue
        path, content = loaded
        for pat, rep in _build_rename_replacements(m.group("old"), m.group("new")):
            content = pat.sub(rep, content)
        file_state[path] = content

    for m in _IMPLICIT_FINAL_DIAGNOSTIC.finditer(xcodebuild_output):
        loaded = load(m.group("file"))
        if loaded is None:
            continue
        path, content = loaded
        file_state[path] = _replace_on_line(
            content, int(m.group("line")), _OPEN_KEYWORD, m.group("modifier")
        )

    for m in _UNAVAILABLE_DIAGNOSTIC.finditer(xcodebuild_output):
        if m.group("symbol") != "characters":
            continue
        loaded = load(m.group("file"))
        if loaded is None:
            continue
        path, content = loaded
        file_state[path] = _replace_on_line(
            content, int(m.group("line")), _DOT_CHARACTERS, ""
        )

    for m in _OPSET_OPERATOR_DIAGNOSTIC.finditer(xcodebuild_output):
        loaded = load(m.group("file"))
        if loaded is None:
            continue
        path, content = loaded
        op = m.group("op")
        method = _OPSET_OPERATOR_METHOD[op]
        # Rewrite ` lhs OP= rhs` → ` lhs.method(rhs)` on the cited line.
        line_pat = re.compile(
            rf"(\b\w+(?:\.\w+)*)\s*{re.escape(op)}\s*([^\n]+?)(\s*(?://[^\n]*)?)$"
        )
        file_state[path] = _replace_on_line(
            content, int(m.group("line")), line_pat, rf"\1.{method}(\2)\3"
        )

    for m in _BANG_NOT_ALLOWED_DIAGNOSTIC.finditer(xcodebuild_output):
        loaded = load(m.group("file"))
        if loaded is None:
            continue
        path, content = loaded
        line_no, col = int(m.group("line")), int(m.group("col"))
        lines = content.split("\n")
        if 1 <= line_no <= len(lines):
            line = lines[line_no - 1]
            idx = col - 1
            # Replace `!` with `?` to preserve the optional type rather than
            # deleting it (which can leave `as CFString` failing to coerce a
            # `String?` LHS).
            if 0 <= idx < len(line) and line[idx] == "!":
                lines[line_no - 1] = line[:idx] + "?" + line[idx + 1 :]
                file_state[path] = "\n".join(lines)

    # Group switch diagnostics by file and apply descending — each insertion
    # adds lines, so processing in source order would shift later line numbers.
    switch_by_file: dict[Path, list[int]] = {}
    for m in _NONEXHAUSTIVE_SWITCH_DIAGNOSTIC.finditer(xcodebuild_output):
        loaded = load(m.group("file"))
        if loaded is None:
            continue
        path, _ = loaded
        switch_by_file.setdefault(path, []).append(int(m.group("line")))
    for path, line_nos in switch_by_file.items():
        content = file_state[path]
        for ln in sorted(set(line_nos), reverse=True):
            content = _add_unknown_default(content, ln)
        file_state[path] = content

    for m in _NOT_OVERRIDABLE_DIAGNOSTIC.finditer(xcodebuild_output):
        loaded = load(m.group("file"))
        if loaded is None:
            continue
        path, content = loaded
        old_member = m.group("old").rsplit(".", 1)[-1]
        new_member = m.group("new").rsplit(".", 1)[-1]
        file_state[path] = _replace_on_line(
            content,
            int(m.group("line")),
            re.compile(rf"\b{re.escape(old_member)}\b"),
            new_member,
        )

    modified = 0
    for path, new_content in file_state.items():
        original = path.read_text()
        if new_content != original:
            _write_patched_source(path, new_content)
            logger.info("Auto-applied fix-its to %s", path.relative_to(work_dir))
            modified += 1
    return modified


_PAGE_LEGACY_OPTIONS_ELSE = re.compile(
    r"#else\n"
    r"((?:[ \t]*///[^\n]*\n)*)"
    r"([ \t]*)var pageViewControllerOptions: \[String:\s*Any\]\? \{"
    r".*?"
    r"\n\2\}\n(?=\s*#endif)",
    re.DOTALL,
)


def _patch_pageboy_management_swift(content: str) -> str:
    """Rewrite Pageboy's legacy ``#else`` options branch.

    Swift 6 type-checks inactive ``#if`` branches, so the legacy
    ``[String: Any]`` + option-key-string path also has to compile.
    """

    def _repl_else(m: re.Match[str]) -> str:
        doc, ind = m.group(1), m.group(2)
        inner = (
            f"{ind}    var options = [UIPageViewController.OptionsKey: Any]()\n"
            f"{ind}    \n"
            f"{ind}    if interPageSpacing > 0.0 {{\n"
            f"{ind}        options[.interPageSpacing] = interPageSpacing\n"
            f"{ind}    }}\n"
            f"{ind}    \n"
            f"{ind}    guard options.count > 0 else {{\n"
            f"{ind}        return nil\n"
            f"{ind}    }}\n"
            f"{ind}    return options\n"
        )
        return (
            f"#else\n{doc}{ind}var pageViewControllerOptions: "
            f"[UIPageViewController.OptionsKey: Any]? {{\n{inner}{ind}}}\n"
        )

    return _PAGE_LEGACY_OPTIONS_ELSE.sub(_repl_else, content)


def _patch_pod_sources(work_dir: Path) -> None:
    """Apply source patches to pinned pod versions incompatible with modern Swift."""
    for rel_path, pattern, replacement in _POD_SOURCE_PATCHES:
        path = work_dir / rel_path
        if not path.exists():
            continue
        content = path.read_text()
        patched = pattern.sub(replacement, content)
        if patched != content:
            _write_patched_source(path, patched)
            logger.info("Patched pod source: %s", rel_path)

    for root_rel in _POD_SOURCE_ROOTS:
        root_dir = work_dir / root_rel
        if not root_dir.is_dir():
            continue
        for swift_file in root_dir.rglob("*.swift"):
            content = swift_file.read_text()
            patched = content
            if (
                swift_file.name == "PageboyViewController+Management.swift"
                and "Pageboy" in swift_file.parts
            ):
                patched = _patch_pageboy_management_swift(patched)
            for pattern, replacement in _POD_SWIFT_RENAMES:
                patched = pattern.sub(replacement, patched)
            for pattern, replacement in _POD_SWIFT_FUNC_TO_PROP:
                patched = pattern.sub(replacement, patched)
            for pattern, replacement in _POD_SWIFT_NOTIFICATION_RENAMES:
                patched = pattern.sub(replacement, patched)
            for pattern, replacement in _POD_SWIFT_PATTERN_RENAMES:
                patched = pattern.sub(replacement, patched)
            if patched != content:
                _write_patched_source(swift_file, patched)
                logger.debug("Applied Swift renames to %s", swift_file)

    _strip_stray_info_plist_from_pods_project(work_dir)


def _lookup_pbx_target_uuid(pbx: str, target_name: str) -> str | None:
    """Return the 24-char UUID of a PBXNativeTarget named *target_name*."""
    if not target_name:
        return None
    m = re.search(
        r"(\w{24}) /\* "
        + re.escape(target_name)
        + r" \*/ = \{\s*isa = PBXNativeTarget;",
        pbx,
    )
    return m.group(1) if m else None


def _lookup_pbx_product_name(pbx: str, target_uuid: str, fallback: str) -> str:
    """Return the app product name (without .app) referenced by *target_uuid*."""
    m = re.search(
        rf"{target_uuid}[^}}]*productReference = \w{{24}} /\* (.+?)\.app \*/",
        pbx,
        re.DOTALL,
    )
    return m.group(1) if m else fallback


def _render_testable_entry(
    test_target: str, test_target_uuid: str, proj_container: str
) -> str:
    return (
        f"         <TestableReference\n"
        f'            skipped = "NO">\n'
        f"            <BuildableReference\n"
        f'               BuildableIdentifier = "primary"\n'
        f'               BlueprintIdentifier = "{test_target_uuid}"\n'
        f'               BuildableName = "{test_target}.xctest"\n'
        f'               BlueprintName = "{test_target}"\n'
        f'               ReferencedContainer = "container:{proj_container}">\n'
        f"            </BuildableReference>\n"
        f"         </TestableReference>\n"
    )


def _render_minimal_scheme(
    scheme_name: str,
    host_target_uuid: str,
    app_product_name: str,
    proj_container: str,
    testable_entries: str,
) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Scheme\n"
        '   LastUpgradeVersion = "1620"\n'
        '   version = "1.7">\n'
        "   <BuildAction\n"
        '      parallelizeBuildables = "YES"\n'
        '      buildImplicitDependencies = "YES">\n'
        "      <BuildActionEntries>\n"
        "         <BuildActionEntry\n"
        '            buildForTesting = "YES"\n'
        '            buildForRunning = "YES"\n'
        '            buildForProfiling = "YES"\n'
        '            buildForArchiving = "YES"\n'
        '            buildForAnalyzing = "YES">\n'
        "            <BuildableReference\n"
        '               BuildableIdentifier = "primary"\n'
        f'               BlueprintIdentifier = "{host_target_uuid}"\n'
        f'               BuildableName = "{app_product_name}.app"\n'
        f'               BlueprintName = "{scheme_name}"\n'
        f'               ReferencedContainer = "container:{proj_container}">\n'
        "            </BuildableReference>\n"
        "         </BuildActionEntry>\n"
        "      </BuildActionEntries>\n"
        "   </BuildAction>\n"
        "   <TestAction\n"
        '      buildConfiguration = "Debug"\n'
        '      selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB"\n'
        '      selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB"\n'
        '      shouldUseLaunchSchemeArgsEnv = "YES">\n'
        "      <Testables>\n"
        f"{testable_entries}"
        "      </Testables>\n"
        "   </TestAction>\n"
        "   <LaunchAction\n"
        '      buildConfiguration = "Debug"\n'
        '      selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB"\n'
        '      selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB"\n'
        '      launchStyle = "0"\n'
        '      useCustomWorkingDirectory = "NO"\n'
        '      ignoresPersistentStateOnLaunch = "NO"\n'
        '      debugDocumentVersioning = "YES"\n'
        '      debugServiceExtension = "internal"\n'
        '      allowLocationSimulation = "YES">\n'
        "      <BuildableProductRunnable\n"
        '         runnableDebuggingMode = "0">\n'
        "         <BuildableReference\n"
        '            BuildableIdentifier = "primary"\n'
        f'            BlueprintIdentifier = "{host_target_uuid}"\n'
        f'            BuildableName = "{app_product_name}.app"\n'
        f'            BlueprintName = "{scheme_name}"\n'
        f'            ReferencedContainer = "container:{proj_container}">\n'
        "         </BuildableReference>\n"
        "      </BuildableProductRunnable>\n"
        "   </LaunchAction>\n"
        "</Scheme>\n"
    )


def _ensure_shared_scheme(
    xcode_config: dict,
    work_dir: Path,
    scheme_name: str,
    test_target: str = "",
) -> None:
    """Generate a minimal shared ``<scheme_name>.xcscheme`` if missing.

    Repos that only commit user-local schemes (xcuserdata) can't be driven by
    xcodebuild; this synthesises a shared scheme pointing at the same target.
    """
    if not scheme_name:
        return

    project_rel, _ = resolve_repo_relative_path(
        xcode_config.get(XCODE_CONFIG_PROJECT, ""),
        work_dir,
    )
    if not project_rel:
        return

    scheme_dir = work_dir / project_rel / "xcshareddata" / "xcschemes"
    scheme_path = scheme_dir / (scheme_name + ".xcscheme")

    pbxproj_path = work_dir / project_rel / PROJECT_PBXPROJ
    if not pbxproj_path.exists():
        return
    pbx = pbxproj_path.read_text()

    proj_container = project_rel.split("/")[-1]
    test_target_uuid = (
        _lookup_pbx_target_uuid(pbx, test_target) if test_target else None
    )
    testable_entry = (
        _render_testable_entry(test_target, test_target_uuid, proj_container)
        if test_target and test_target_uuid
        else ""
    )

    if scheme_path.exists():
        if testable_entry:
            scheme_xml = scheme_path.read_text()
            if test_target not in scheme_xml:
                if "      <Testables>\n      </Testables>" in scheme_xml:
                    scheme_xml = scheme_xml.replace(
                        "      <Testables>\n      </Testables>",
                        f"      <Testables>\n{testable_entry}      </Testables>",
                    )
                elif "      </Testables>" in scheme_xml:
                    scheme_xml = scheme_xml.replace(
                        "      </Testables>",
                        f"{testable_entry}      </Testables>",
                    )
                scheme_path.write_text(scheme_xml)
        return

    host_target_uuid = _lookup_pbx_target_uuid(pbx, scheme_name)
    if not host_target_uuid:
        return
    app_product_name = _lookup_pbx_product_name(pbx, host_target_uuid, scheme_name)

    scheme_dir.mkdir(parents=True, exist_ok=True)
    scheme_path.write_text(
        _render_minimal_scheme(
            scheme_name,
            host_target_uuid,
            app_product_name,
            proj_container,
            testable_entry,
        )
    )
    logger.info("Created minimal shared scheme at %s", scheme_path)


def get_test_destination(xcode_config: dict) -> str:
    """Resolve test_destination from config (test_destination or destination)."""
    return xcode_config.get(
        "test_destination",
        xcode_config.get("destination", ""),
    )


def get_app_test_destination(xcode_config: dict) -> str:
    """Resolve app_test_destination from config (app_test_destination or test_destination or destination)."""
    return xcode_config.get(
        "app_test_destination",
        get_test_destination(xcode_config),
    )


def get_app_bundle_name(xcode_config: dict) -> str:
    """Resolve app bundle name from config (app_bundle_name or scheme)."""
    return xcode_config.get("app_bundle_name") or xcode_config.get(
        XCODE_CONFIG_SCHEME, ""
    )


def resolve_test_package_path(xcode_config: dict, work_dir: Path) -> str:
    """Resolve ``test_package_path`` to the first candidate that exists.

    ``test_package_path`` may be a single string or a list of candidate paths.
    Returns the first path where ``Package.swift`` exists under *work_dir*,
    or an empty string if none match.
    """
    raw = xcode_config.get("test_package_path", "")
    if not raw:
        return ""
    candidates = raw if isinstance(raw, list) else [raw]
    for candidate in candidates:
        if (work_dir / candidate / "Package.swift").exists():
            return candidate
    return ""


def resolve_repo_relative_path(
    config_path: str, work_dir: Path
) -> tuple[str, Path | None]:
    """Resolve a repo-relative config path under a worktree."""
    rel = (config_path or "").strip()
    if not rel:
        return "", None

    rel_path = Path(rel)
    direct = work_dir / rel_path
    if direct.exists():
        return rel, direct

    parts = rel_path.parts
    if len(parts) > 1:
        stripped_rel = Path(*parts[1:]).as_posix()
        stripped = work_dir / stripped_rel
        if stripped.exists():
            logger.warning(
                "Configured path '%s' not found in %s; using '%s'",
                rel,
                work_dir,
                stripped_rel,
            )
            return stripped_rel, stripped

    return rel, direct


class XcodeBuildCache:
    """Manages pre-built DerivedData caches per (repo, base_commit) pair."""

    def __init__(self, cache_root: Path | None = None):
        self.cache_root = cache_root or _default_cache_root()
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def _repo_cache_dir(self, repo_name: str) -> Path:
        return self.cache_root / repo_name

    def commit_cache_dir(self, repo_name: str, base_commit: str) -> Path:
        short = base_commit[:12]
        return self._repo_cache_dir(repo_name) / short

    def repo_clone_dir(self, repo_name: str) -> Path:
        return self._repo_cache_dir(repo_name) / "_repo"

    def _derived_data_dir(self, repo_name: str, base_commit: str) -> Path:
        return self.commit_cache_dir(repo_name, base_commit) / _DD_DIR

    def _test_derived_data_dir(self, repo_name: str, base_commit: str) -> Path:
        return self.commit_cache_dir(repo_name, base_commit) / _TEST_DD_DIR

    def _app_test_derived_data_dir(self, repo_name: str, base_commit: str) -> Path:
        return self.commit_cache_dir(repo_name, base_commit) / _APP_TEST_DD_DIR

    def warm_worktree_path(self, repo_name: str, base_commit: str) -> Path:
        """Return the canonical worktree path used during cache warming."""
        return self.commit_cache_dir(repo_name, base_commit) / "worktree"

    def eval_worktree_path(
        self,
        repo_name: str,
        base_commit: str,
        instance_id: str,
        attempt: int | None,
    ) -> Path:
        """Isolated worktree path for eval_single_patch."""
        label = (
            f"{instance_id}:attempt_{attempt}" if attempt is not None else instance_id
        )
        safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in label)
        return self.commit_cache_dir(repo_name, base_commit) / "eval-worktrees" / safe

    def warm_app_test_dd_path(self, repo_name: str, base_commit: str) -> Path:
        """Return the app-test DerivedData path built during cache warming."""
        return self._app_test_derived_data_dir(repo_name, base_commit)

    def prepare_eval_app_test_derived_data(
        self,
        repo_name: str,
        base_commit: str,
        worktree_dir: Path,
        xcode_config: dict | None = None,
    ) -> Path:
        """Return a worktree-local DerivedData path for app/UI xcodebuild test runs."""
        dest = worktree_dir / _APP_TEST_DD_DIR

        # When skip_warm_app_test_dd is set in xcode_config, don't clone the
        # warm DerivedData.
        if xcode_config and xcode_config.get("skip_warm_app_test_dd"):
            logger.warning(
                "skip_warm_app_test_dd: skipping warm DD clone for %s", worktree_dir
            )
            # If a previous clone left stale DD, remove it
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            return dest

        warm = self._app_test_derived_data_dir(repo_name, base_commit)
        _clone_dd_if_populated(warm, dest)
        module_cache = dest / "ModuleCache.noindex"
        if module_cache.exists():
            shutil.rmtree(module_cache, ignore_errors=True)
        source_packages = dest / "SourcePackages"
        if source_packages.exists():
            shutil.rmtree(source_packages, ignore_errors=True)
        return dest

    def _expected_test_dd_dirs(
        self, xcode_config: dict, repo_name: str, base_commit: str
    ) -> list[Path]:
        """Return test DD directories configured for this repo/commit."""
        dirs: list[Path] = []
        if xcode_config.get(XCODE_CONFIG_TEST_SCHEME):
            dirs.append(self._test_derived_data_dir(repo_name, base_commit))
        if xcode_config.get(XCODE_CONFIG_APP_TEST_SCHEME):
            dirs.append(self._app_test_derived_data_dir(repo_name, base_commit))
        return dirs

    def is_warm(self, repo_name: str, base_commit: str) -> bool:
        return _dd_is_populated(self._derived_data_dir(repo_name, base_commit))

    def ensure_cloned(self, repo_name: str, repo_path: Path) -> None:
        """Clone the repo into the cache and fetch all commits.

        Call once per repo before warming commits in parallel to avoid
        concurrent clone/fetch races when multiple commits share a repo.
        """
        clone_dir = self.repo_clone_dir(repo_name)
        if not clone_dir.exists():
            typer.echo(f"  Cloning {repo_name} into cache...")
            _run_cmd(["git", "clone", str(repo_path.resolve()), str(clone_dir)])
        typer.echo(f"  Fetching {repo_name}...")
        _run_cmd(["git", "-C", str(clone_dir), "fetch", "--all"], check=False)

    def warm(
        self,
        repo_path: Path,
        repo_name: str,
        base_commit: str,
        xcode_config: dict,
    ) -> Path:
        """Pre-build a base commit and cache DerivedData.

        Returns the path to the cached DerivedData directory.
        """
        dd_dir = self._derived_data_dir(repo_name, base_commit)
        build_cached = self.is_warm(repo_name, base_commit)

        commit_dir = self.commit_cache_dir(repo_name, base_commit)
        commit_dir.mkdir(parents=True, exist_ok=True)

        clone_dir = self.repo_clone_dir(repo_name)

        # Check if test DDs need warming even when the main build is cached.
        needs_test_warm = self._needs_test_warm(xcode_config, repo_name, base_commit)

        if build_cached and not needs_test_warm:
            logger.info("Cache hit for %s@%s", repo_name, base_commit[:8])
            return dd_dir

        if not clone_dir.exists():
            typer.echo(f"  Cloning {repo_name} into cache...")
            _run_cmd(["git", "clone", str(repo_path.resolve()), str(clone_dir)])
            _run_cmd(["git", "-C", str(clone_dir), "fetch", "--all"], check=False)

        work_dir = commit_dir / "worktree"
        if work_dir.exists():
            _remove_worktree(clone_dir, work_dir)

        typer.echo(f"  Creating worktree at {base_commit[:8]}...")
        _run_cmd(
            [
                "git",
                "-C",
                str(clone_dir),
                "worktree",
                "add",
                "--detach",
                str(work_dir),
                base_commit,
            ]
        )

        _patch_podfile_build_settings(
            work_dir, _MIN_IPHONEOS_DEPLOYMENT_TARGET, _MIN_SWIFT_VERSION
        )
        _bump_main_project_min_ios(
            xcode_config, work_dir, _MIN_IPHONEOS_DEPLOYMENT_TARGET
        )
        _run_pre_build_commands(xcode_config, work_dir)
        _patch_pod_sources(work_dir)
        _ensure_shared_scheme(
            xcode_config, work_dir, xcode_config.get(XCODE_CONFIG_SCHEME, "")
        )

        if not build_cached:
            typer.echo(f"  Building {repo_name} (full clean build)...")
            dd_dir.mkdir(parents=True, exist_ok=True)

            build_cmd = _build_xcodebuild_cmd(
                xcode_config,
                work_dir,
                dd_dir,
                clean=True,
                allow_pkg_resolution=True,
            )
            build_timeout = _build_timeout(xcode_config)
            try:
                result = _run_xcodebuild(build_cmd, str(work_dir), build_timeout)
            except subprocess.TimeoutExpired:
                # Clean up the partial DerivedData so is_warm() won't falsely
                # report this commit as cached on subsequent warm runs.
                shutil.rmtree(dd_dir, ignore_errors=True)
                raise

            if result.returncode != 0 and _is_package_resolution_failure(
                result.stdout, result.stderr
            ):
                typer.echo(
                    f"  Package resolution failed for {repo_name}@{base_commit[:8]}"
                    " — retrying once...",
                    err=True,
                )
                resolve_cmd = _build_resolve_packages_cmd(xcode_config, work_dir)
                _run_xcodebuild(resolve_cmd, str(work_dir), build_timeout)
                result = _run_xcodebuild(build_cmd, str(work_dir), build_timeout)

            # Loop: each pass can expose new "renamed to" diagnostics.
            for _ in range(_RENAME_FIXIT_MAX_PASSES):
                if result.returncode == 0:
                    break
                modified = _apply_compiler_fixits(
                    (result.stdout or "") + "\n" + (result.stderr or ""), work_dir
                )
                if not modified:
                    break
                shutil.rmtree(dd_dir, ignore_errors=True)
                dd_dir.mkdir(parents=True, exist_ok=True)
                result = _run_xcodebuild(build_cmd, str(work_dir), build_timeout)

            if result.returncode != 0:
                summary = _format_build_errors(result.stderr, result.stdout)
                shutil.rmtree(dd_dir, ignore_errors=True)
                raise RuntimeError(
                    f"Main build failed for {repo_name}@{base_commit[:8]}:\n{summary}"
                )

        self._warm_test_dd(xcode_config, work_dir, repo_name, base_commit)
        self._save_package_resolved(xcode_config, work_dir, repo_name, base_commit)

        _remove_worktree(clone_dir, work_dir)

        typer.echo(f"  Cached DerivedData for {repo_name}@{base_commit[:8]}")
        return dd_dir

    @staticmethod
    def _package_resolved_path(xcode_config: dict, work_dir: Path) -> Path | None:
        """Return the authoritative Package.resolved path for the build.

        When a workspace is configured, xcodebuild uses the workspace-level
        ``Package.resolved`` (``<workspace>/xcshareddata/swiftpm/...``) and
        ignores the project's copy.  Fall back to the project's when no
        workspace is set (or the workspace file is absent on disk).
        """
        workspace_rel, workspace_path = resolve_repo_relative_path(
            xcode_config.get("workspace", ""),
            work_dir,
        )
        if workspace_rel and workspace_path and workspace_path.exists():
            return workspace_path / "xcshareddata" / "swiftpm" / "Package.resolved"
        project_rel, _ = resolve_repo_relative_path(
            xcode_config.get(XCODE_CONFIG_PROJECT, ""),
            work_dir,
        )
        if not project_rel:
            return None
        return (
            work_dir
            / project_rel
            / "project.xcworkspace"
            / "xcshareddata"
            / "swiftpm"
            / "Package.resolved"
        )

    def _save_package_resolved(
        self,
        xcode_config: dict,
        work_dir: Path,
        repo_name: str,
        base_commit: str,
    ) -> None:
        """Copy Package.resolved from the worktree into the cache."""
        src = self._package_resolved_path(xcode_config, work_dir)
        if not src or not src.exists():
            return
        dst = self.commit_cache_dir(repo_name, base_commit) / "Package.resolved"
        shutil.copy2(src, dst)
        logger.info("Saved Package.resolved for %s@%s", repo_name, base_commit[:8])

    def _restore_package_resolved(
        self,
        xcode_config: dict,
        repo_name: str,
        base_commit: str,
        target_dir: Path,
    ) -> None:
        """Restore cached Package.resolved into a checkout."""
        src = self.commit_cache_dir(repo_name, base_commit) / "Package.resolved"
        if not src.exists():
            return
        dst = self._package_resolved_path(xcode_config, target_dir)
        if not dst:
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        logger.info("Restored Package.resolved for %s@%s", repo_name, base_commit[:8])

    def _needs_test_warm(
        self, xcode_config: dict, repo_name: str, base_commit: str
    ) -> bool:
        """Check if any test DerivedData directories need warming."""
        return any(
            not _dd_is_populated(path)
            for path in self._expected_test_dd_dirs(
                xcode_config, repo_name, base_commit
            )
        )

    def _warm_test_dd(
        self,
        xcode_config: dict,
        work_dir: Path,
        repo_name: str,
        base_commit: str,
    ) -> None:
        """Pre-build test schemes so eval runs skip dependency resolution."""
        self._warm_spm_test_dd(xcode_config, work_dir, repo_name, base_commit)
        self._warm_app_test_dd(xcode_config, work_dir, repo_name, base_commit)

    def _warm_spm_test_dd(
        self,
        xcode_config: dict,
        work_dir: Path,
        repo_name: str,
        base_commit: str,
    ) -> None:
        """Pre-build the SPM test scheme so eval runs skip dependency resolution."""
        test_scheme = xcode_config.get(XCODE_CONFIG_TEST_SCHEME, "")
        if not test_scheme:
            return

        test_dd_dir = self._test_derived_data_dir(repo_name, base_commit)
        if _dd_is_populated(test_dd_dir):
            return

        resolved_pkg = resolve_test_package_path(xcode_config, work_dir)
        if not resolved_pkg:
            return

        test_files_dest = xcode_config.get(XCODE_CONFIG_TEST_FILES_DEST, "")
        if not test_files_dest:
            return

        dummy_dir = work_dir / resolved_pkg / test_files_dest
        dummy_dir.mkdir(parents=True, exist_ok=True)
        dummy_file = dummy_dir / _WARMUP_FILENAME
        dummy_file.write_text("import XCTest\nclass AnvilWarmupTests: XCTestCase {}\n")

        test_dd_dir.mkdir(parents=True, exist_ok=True)
        # Warming is the one moment it's safe to resolve packages — the SPM
        # test package is standalone and may have no checked-in
        # Package.resolved (or the checked-in one may be stale after our
        # pre-build pin edits).  Eval runs re-use the warmed DD and keep
        # resolution disabled for determinism.
        test_cmd_info = _build_xcodebuild_test_cmd(
            xcode_config, work_dir, test_dd_dir, allow_pkg_resolution=True
        )
        if not test_cmd_info:
            dummy_file.unlink(missing_ok=True)
            return

        test_cmd, test_cwd = test_cmd_info
        test_cmd = _as_build_for_testing(test_cmd)

        build_timeout = _build_timeout(xcode_config)
        typer.echo(f"  Warming test DerivedData for {repo_name}@{base_commit[:8]}...")
        try:
            result = _run_xcodebuild(test_cmd, str(test_cwd), build_timeout)
        except subprocess.TimeoutExpired:
            dummy_file.unlink(missing_ok=True)
            shutil.rmtree(test_dd_dir, ignore_errors=True)
            raise RuntimeError(
                f"Test DerivedData warm timed out for {repo_name}@{base_commit[:8]}"
            )
        dummy_file.unlink(missing_ok=True)

        if result.returncode != 0:
            summary = _format_build_errors(
                result.stderr,
                result.stdout,
                max_lines=5,
                fallback_chars=300,
            )
            shutil.rmtree(test_dd_dir, ignore_errors=True)
            raise RuntimeError(
                f"Test DerivedData warm failed for {repo_name}@{base_commit[:8]}:\n"
                f"{summary}"
            )

    def _warm_app_test_dd(
        self,
        xcode_config: dict,
        work_dir: Path,
        repo_name: str,
        base_commit: str,
    ) -> None:
        """Pre-build the app-level test scheme (``DerivedData-app-tests``)."""
        app_test_scheme = xcode_config.get(XCODE_CONFIG_APP_TEST_SCHEME, "")
        if not app_test_scheme:
            return

        app_test_dd = self._app_test_derived_data_dir(repo_name, base_commit)
        if _dd_is_populated(app_test_dd):
            return

        app_test_target = xcode_config.get(XCODE_CONFIG_APP_TEST_TARGET, "")
        app_test_files_dest = xcode_config.get(XCODE_CONFIG_APP_TEST_FILES_DEST, "")
        if not app_test_target or not app_test_files_dest:
            return

        inject_app_test_target(xcode_config, work_dir)
        _ensure_shared_scheme(
            xcode_config, work_dir, app_test_scheme, test_target=app_test_target
        )

        dummy_dir = work_dir / app_test_files_dest
        dummy_dir.mkdir(parents=True, exist_ok=True)
        dummy_file = dummy_dir / _WARMUP_FILENAME
        dummy_file.write_text(
            "import XCTest\nclass AnvilAppWarmupTests: XCTestCase {\n"
            "    func testWarmup() { XCTAssertTrue(true) }\n}\n"
        )

        app_test_dd.mkdir(parents=True, exist_ok=True)
        cmd_info = _build_xcodebuild_app_test_cmd(
            xcode_config,
            work_dir,
            app_test_dd,
            allow_pkg_resolution=True,
        )
        if not cmd_info:
            dummy_file.unlink(missing_ok=True)
            return

        cmd, cwd = cmd_info
        cmd = _as_build_for_testing(cmd)

        build_timeout = _build_timeout(xcode_config)
        typer.echo(
            f"  Warming app-test DerivedData for {repo_name}@{base_commit[:8]}..."
        )
        try:
            result = _run_xcodebuild(cmd, str(cwd), build_timeout)
        except subprocess.TimeoutExpired:
            dummy_file.unlink(missing_ok=True)
            shutil.rmtree(app_test_dd, ignore_errors=True)
            raise RuntimeError(
                f"App-test DerivedData warm timed out for {repo_name}@{base_commit[:8]}"
            )
        dummy_file.unlink(missing_ok=True)

        if result.returncode != 0:
            summary = _format_build_errors(
                result.stderr,
                result.stdout,
                max_lines=5,
                fallback_chars=300,
            )
            shutil.rmtree(app_test_dd, ignore_errors=True)
            raise RuntimeError(
                f"App-test DerivedData warm failed for {repo_name}@{base_commit[:8]}:\n"
                f"{summary}"
            )

    def checkout(
        self,
        repo_name: str,
        base_commit: str,
        target_dir: Path,
        xcode_config: dict | None = None,
        copy_derived_data: bool = True,
        run_pre_build: bool = True,
    ) -> Path:
        """Create an isolated worktree with pre-built DerivedData.

        Returns target_dir (the worktree root).

        When copy_derived_data=False the DerivedData directories are not copied
        into target_dir — useful when running the eval from the same path used
        during warming so that Xcode can reuse compiled products via exact path
        matching.
        """
        clone_dir = self.repo_clone_dir(repo_name)
        if not clone_dir.exists():
            raise RuntimeError(
                f"No cached repo for {repo_name}. Run 'anvil warm-xcode-cache' first."
            )

        if target_dir.exists():
            self.cleanup(repo_name, target_dir)

        _run_cmd(
            [
                "git",
                "-C",
                str(clone_dir),
                "worktree",
                "add",
                "--detach",
                str(target_dir),
                base_commit,
            ]
        )

        if copy_derived_data:
            # Clone each DerivedData dir and strip ModuleCache so Xcode rebuilds it
            # cheaply while still reusing compiled products.
            for dd_name, cache_dd in [
                (_DD_DIR, self._derived_data_dir(repo_name, base_commit)),
                (_TEST_DD_DIR, self._test_derived_data_dir(repo_name, base_commit)),
                (
                    _APP_TEST_DD_DIR,
                    self._app_test_derived_data_dir(repo_name, base_commit),
                ),
            ]:
                _clone_dd_if_populated(cache_dd, target_dir / dd_name)
                module_cache = target_dir / dd_name / "ModuleCache.noindex"
                if module_cache.exists():
                    shutil.rmtree(module_cache, ignore_errors=True)

        if xcode_config:
            if run_pre_build:
                _patch_podfile_build_settings(
                    target_dir, _MIN_IPHONEOS_DEPLOYMENT_TARGET, _MIN_SWIFT_VERSION
                )
                _bump_main_project_min_ios(
                    xcode_config, target_dir, _MIN_IPHONEOS_DEPLOYMENT_TARGET
                )
                _run_pre_build_commands(xcode_config, target_dir)
                _patch_pod_sources(target_dir)
            self._restore_package_resolved(
                xcode_config, repo_name, base_commit, target_dir
            )

        return target_dir

    def cleanup(self, repo_name: str, target_dir: Path) -> None:
        """Remove a worktree created by checkout()."""
        clone_dir = self.repo_clone_dir(repo_name)
        if clone_dir.exists():
            _run_cmd(
                [
                    "git",
                    "-C",
                    str(clone_dir),
                    "worktree",
                    "remove",
                    "--force",
                    str(target_dir),
                ],
                check=False,
            )
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)


# Shared xcodebuild flags used across build/test commands.
_XCODEBUILD_NO_SIGN_FLAGS = [
    "-skipPackagePluginValidation",
    "ONLY_ACTIVE_ARCH=YES",
    "CODE_SIGNING_ALLOWED=NO",
    "CODE_SIGN_IDENTITY=",
    "COMPILER_INDEX_STORE_ENABLE=NO",
]
# App-hosted tests need ad-hoc signing so entitlements (e.g. CloudKit) are preserved.
_XCODEBUILD_ADHOC_SIGN_FLAGS = [
    "-skipPackagePluginValidation",
    "ONLY_ACTIVE_ARCH=YES",
    "CODE_SIGNING_ALLOWED=YES",
    "CODE_SIGN_IDENTITY=-",
    "COMPILER_INDEX_STORE_ENABLE=NO",
]


def _as_build_for_testing(cmd: list[str]) -> list[str]:
    """Replace the 'test' action with 'build-for-testing' in an xcodebuild command."""
    return ["build-for-testing" if c == "test" else c for c in cmd]


def _resolve_project_args(xcode_config: dict, work_dir: Path) -> list[str]:
    """Resolve -workspace/-project args, preferring workspace when it exists."""
    workspace, workspace_path = resolve_repo_relative_path(
        xcode_config.get("workspace", ""),
        work_dir,
    )
    project, project_path = resolve_repo_relative_path(
        xcode_config.get(XCODE_CONFIG_PROJECT, ""),
        work_dir,
    )

    if workspace_path and workspace_path.exists():
        return ["-workspace", str(workspace_path)]
    elif project_path and project_path.exists():
        return ["-project", str(project_path)]
    elif workspace:
        return ["-workspace", str(workspace_path)]
    elif project:
        return ["-project", str(project_path)]
    return []


def _build_xcodebuild_cmd(
    xcode_config: dict,
    work_dir: Path,
    derived_data_dir: Path,
    clean: bool = False,
    allow_pkg_resolution: bool = False,
) -> list[str]:
    """Build the xcodebuild compile command from config."""
    scheme = xcode_config["scheme"]
    destination = xcode_config.get(
        "destination",
        "generic/platform=iOS Simulator",
    )

    cmd = ["xcodebuild"]
    if clean:
        cmd.append("clean")
    cmd.append("build")

    cmd.extend(_resolve_project_args(xcode_config, work_dir))
    cmd.extend(
        [
            "-scheme",
            scheme,
            "-destination",
            destination,
            "-derivedDataPath",
            str(derived_data_dir),
            "-quiet",
            *_XCODEBUILD_NO_SIGN_FLAGS,
        ]
    )
    if not allow_pkg_resolution:
        cmd.append("-disableAutomaticPackageResolution")

    cmd.extend(xcode_config.get("extra_build_flags", []))

    return cmd


def _build_resolve_packages_cmd(xcode_config: dict, work_dir: Path) -> list[str]:
    """Build an xcodebuild command that resolves package dependencies only."""
    cmd = ["xcodebuild", "-resolvePackageDependencies"]
    cmd.extend(_resolve_project_args(xcode_config, work_dir))
    scheme = xcode_config.get(XCODE_CONFIG_SCHEME, "")
    if scheme:
        cmd.extend(["-scheme", scheme])
    cmd.extend(xcode_config.get("extra_build_flags", []))
    return cmd


def _build_xcodebuild_test_cmd(
    xcode_config: dict,
    work_dir: Path,
    derived_data_dir: Path,
    test_only: list[str] | None = None,
    allow_pkg_resolution: bool = False,
) -> tuple[list[str], Path] | None:
    """Build the xcodebuild test command.

    Returns (cmd, cwd) or None if no test config.

    When ``test_package_path`` is set in the config, the test command targets
    the standalone SPM package (no -project/-workspace flags) and ``cwd`` is
    set to the package directory.  Otherwise the main project is used and
    ``cwd`` is the worktree root.

    Args:
        test_only: Optional list of test identifiers to run.
        allow_pkg_resolution: If True, omit ``-disableAutomaticPackageResolution``
            (used during cache warming).
    """
    test_scheme = xcode_config.get(XCODE_CONFIG_TEST_SCHEME, "")
    if not test_scheme:
        return None

    test_destination = get_test_destination(xcode_config)
    if not test_destination or "generic/" in test_destination:
        return None

    test_package_path = resolve_test_package_path(xcode_config, work_dir)

    cmd = ["xcodebuild", "test"]

    if test_package_path:
        cwd = work_dir / test_package_path
    elif xcode_config.get("test_package_path"):
        logger.warning(
            "No test_package_path candidate has Package.swift at %s — skipping tests",
            work_dir,
        )
        return None
    else:
        cwd = work_dir
        cmd.extend(_resolve_project_args(xcode_config, work_dir))

    cmd.extend(
        [
            "-scheme",
            test_scheme,
            "-destination",
            test_destination,
            "-derivedDataPath",
            str(derived_data_dir),
            *_XCODEBUILD_NO_SIGN_FLAGS,
        ]
    )
    if not allow_pkg_resolution:
        cmd.append("-disableAutomaticPackageResolution")

    cmd.extend(xcode_config.get("extra_build_flags", []))

    only = test_only or xcode_config.get("test_only", [])
    for target in only:
        cmd.extend(["-only-testing:" + target])

    return cmd, cwd


def _inject_test_target(
    xcode_config: dict,
    work_dir: Path,
    *,
    test_target: str,
    files_dest: str,
    project_rel: str,
    bundle_id: str,
    scheme_name: str,
    product_type: str,
    is_ui_test: bool,
) -> bool:
    """Inject a test target (unit-test or UI-test) into the Xcode project.

    Writes pbxproj sections, wires the scheme's TestAction, creates Info.plist
    and a placeholder Swift file.  Used by both :func:`inject_app_test_target`
    (unit tests) and :func:`inject_ui_test_target` (UI tests).

    Returns True on success, False when skipped or failed.
    """
    if not test_target or not files_dest or not project_rel:
        return False

    project_rel, _ = resolve_repo_relative_path(project_rel, work_dir)
    pbxproj_path = work_dir / project_rel / PROJECT_PBXPROJ
    if not pbxproj_path.exists():
        logger.warning("project.pbxproj not found at %s", pbxproj_path)
        return False

    pbx = pbxproj_path.read_text()
    if test_target in pbx:
        logger.debug("Target %s already exists, skipping injection", test_target)
        return True

    uid = {
        k: _pbx_uuid(f"{test_target}-{k}")
        for k in [
            "group",
            "info_plist_ref",
            "placeholder_ref",
            "placeholder_build",
            "product_ref",
            "sources_phase",
            "resources_phase",
            "frameworks_phase",
            "target",
            "config_debug",
            "config_release",
            "config_list",
            "target_dep",
            "container_proxy",
        ]
    }

    # Discover the host app target UUID and project object UUID from pbxproj
    m = re.search(
        r"(\w{24}) /\* "
        + re.escape(xcode_config.get(XCODE_CONFIG_SCHEME, ""))
        + r" \*/ = \{\s*isa = PBXNativeTarget;",
        pbx,
    )
    if not m:
        logger.warning("Could not find host app target in pbxproj")
        return False
    host_target_uuid = m.group(1)

    m = re.search(r"rootObject = (\w{24})", pbx)
    if not m:
        return False
    project_uuid = m.group(1)

    # Find the Products group UUID
    m = re.search(r"productRefGroup = (\w{24})", pbx)
    products_group_uuid = m.group(1) if m else None

    # Find the main group UUID
    m = re.search(r"mainGroup = (\w{24})", pbx)
    main_group_uuid = m.group(1) if m else None

    # Discover the app's PRODUCT_NAME for TEST_HOST (unit tests only)
    m = re.search(r"productReference = \w{24} /\* (.+?)\.app \*/", pbx)
    app_product_name = m.group(1) if m else xcode_config.get(XCODE_CONFIG_SCHEME, "")

    # 1. PBXBuildFile
    pbx = pbx.replace(
        "/* End PBXBuildFile section */",
        f"\t\t{uid['placeholder_build']} /* {test_target}Placeholder.swift in Sources */  = "
        f"{{isa = PBXBuildFile; fileRef = {uid['placeholder_ref']} /* {test_target}Placeholder.swift */; }};\n"
        "/* End PBXBuildFile section */",
    )

    # 2. PBXContainerItemProxy
    proxy_block = (
        f"\t\t{uid['container_proxy']} /* PBXContainerItemProxy */ = {{\n"
        f"\t\t\tisa = PBXContainerItemProxy;\n"
        f"\t\t\tcontainerPortal = {project_uuid} /* Project object */;\n"
        f"\t\t\tproxyType = 1;\n"
        f"\t\t\tremoteGlobalIDString = {host_target_uuid};\n"
        f"\t\t\tremoteInfo = {xcode_config.get('scheme', '')};\n"
        f"\t\t}};\n"
    )
    if "/* End PBXContainerItemProxy section */" in pbx:
        pbx = pbx.replace(
            "/* End PBXContainerItemProxy section */",
            proxy_block + "/* End PBXContainerItemProxy section */",
        )
    else:
        pbx = pbx.replace(
            "/* Begin PBXCopyFilesBuildPhase section */",
            "/* Begin PBXContainerItemProxy section */\n"
            + proxy_block
            + "/* End PBXContainerItemProxy section */\n\n"
            "/* Begin PBXCopyFilesBuildPhase section */",
        )

    # 3. PBXFileReference
    pbx = pbx.replace(
        "/* End PBXFileReference section */",
        f"\t\t{uid['info_plist_ref']} /* Info.plist */ = "
        f'{{isa = PBXFileReference; lastKnownFileType = text.plist.xml; path = Info.plist; sourceTree = "<group>"; }};\n'
        f"\t\t{uid['placeholder_ref']} /* {test_target}Placeholder.swift */ = "
        f'{{isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = {test_target}Placeholder.swift; sourceTree = "<group>"; }};\n'
        f"\t\t{uid['product_ref']} /* {test_target}.xctest */ = "
        f"{{isa = PBXFileReference; explicitFileType = wrapper.cfbundle; includeInIndex = 0; path = {test_target}.xctest; sourceTree = BUILT_PRODUCTS_DIR; }};\n"
        "/* End PBXFileReference section */",
    )

    # 4. PBXFrameworksBuildPhase
    pbx = pbx.replace(
        "/* End PBXFrameworksBuildPhase section */",
        f"\t\t{uid['frameworks_phase']} /* Frameworks */ = {{\n"
        f"\t\t\tisa = PBXFrameworksBuildPhase;\n"
        f"\t\t\tbuildActionMask = {_PBX_BUILD_ACTION_MASK};\n"
        f"\t\t\tfiles = (\n\t\t\t);\n"
        f"\t\t\trunOnlyForDeploymentPostprocessing = 0;\n\t\t}};\n"
        "/* End PBXFrameworksBuildPhase section */",
    )

    # 5. PBXGroup for the test target
    pbx = pbx.replace(
        "/* End PBXGroup section */",
        f"\t\t{uid['group']} /* {test_target} */ = {{\n"
        f"\t\t\tisa = PBXGroup;\n"
        f"\t\t\tchildren = (\n"
        f"\t\t\t\t{uid['info_plist_ref']} /* Info.plist */,\n"
        f"\t\t\t\t{uid['placeholder_ref']} /* {test_target}Placeholder.swift */,\n"
        f"\t\t\t);\n"
        f"\t\t\tpath = {test_target};\n"
        f'\t\t\tsourceTree = "<group>";\n\t\t}};\n'
        "/* End PBXGroup section */",
    )

    # Add group to main group and product to Products group
    if main_group_uuid and products_group_uuid:
        pbx = pbx.replace(
            f"{products_group_uuid} /* Products */,",
            f"{uid['group']} /* {test_target} */,\n\t\t\t\t{products_group_uuid} /* Products */,",
            1,
        )
        products_children_end = re.search(
            rf"{products_group_uuid} /\* Products \*/ = \{{\s*isa = PBXGroup;\s*children = \((.*?)\);",
            pbx,
            re.DOTALL,
        )
        if products_children_end:
            insert_pos = products_children_end.end(1)
            pbx = (
                pbx[:insert_pos]
                + f"\n\t\t\t\t{uid['product_ref']} /* {test_target}.xctest */,"
                + pbx[insert_pos:]
            )

    # 6. PBXNativeTarget
    pbx = pbx.replace(
        "/* End PBXNativeTarget section */",
        f"\t\t{uid['target']} /* {test_target} */ = {{\n"
        f"\t\t\tisa = PBXNativeTarget;\n"
        f'\t\t\tbuildConfigurationList = {uid["config_list"]} /* Build configuration list for PBXNativeTarget "{test_target}" */;\n'
        f"\t\t\tbuildPhases = (\n"
        f"\t\t\t\t{uid['sources_phase']} /* Sources */,\n"
        f"\t\t\t\t{uid['frameworks_phase']} /* Frameworks */,\n"
        f"\t\t\t\t{uid['resources_phase']} /* Resources */,\n"
        f"\t\t\t);\n"
        f"\t\t\tbuildRules = (\n\t\t\t);\n"
        f"\t\t\tdependencies = (\n"
        f"\t\t\t\t{uid['target_dep']} /* PBXTargetDependency */,\n"
        f"\t\t\t);\n"
        f"\t\t\tname = {test_target};\n"
        f"\t\t\tproductName = {test_target};\n"
        f"\t\t\tproductReference = {uid['product_ref']} /* {test_target}.xctest */;\n"
        f'\t\t\tproductType = "{product_type}";\n'
        f"\t\t}};\n"
        "/* End PBXNativeTarget section */",
    )

    # 7. Add to project targets list
    pbx = re.sub(
        rf"(targets = \([^)]*{re.escape(host_target_uuid)}[^)]*)\);",
        rf"\1\t\t\t\t{uid['target']} /* {test_target} */,\n\t\t\t);",
        pbx,
        count=1,
    )

    # 8. TargetAttributes
    m = re.search(r"(TargetAttributes = \{.*?)((\s*\};){2})", pbx, re.DOTALL)
    if m:
        insert_at = m.start(2)
        attr_block = (
            f"\n\t\t\t\t\t{uid['target']} = {{\n"
            f"\t\t\t\t\t\tCreatedOnToolsVersion = 12.0;\n"
            f"\t\t\t\t\t\tTestTargetID = {host_target_uuid};\n"
            f"\t\t\t\t\t}};"
        )
        pbx = pbx[:insert_at] + attr_block + pbx[insert_at:]

    # 9. PBXResourcesBuildPhase
    pbx = pbx.replace(
        "/* End PBXResourcesBuildPhase section */",
        f"\t\t{uid['resources_phase']} /* Resources */ = {{\n"
        f"\t\t\tisa = PBXResourcesBuildPhase;\n"
        f"\t\t\tbuildActionMask = {_PBX_BUILD_ACTION_MASK};\n"
        f"\t\t\tfiles = (\n\t\t\t);\n"
        f"\t\t\trunOnlyForDeploymentPostprocessing = 0;\n\t\t}};\n"
        "/* End PBXResourcesBuildPhase section */",
    )

    # 10. PBXSourcesBuildPhase
    pbx = pbx.replace(
        "/* End PBXSourcesBuildPhase section */",
        f"\t\t{uid['sources_phase']} /* Sources */ = {{\n"
        f"\t\t\tisa = PBXSourcesBuildPhase;\n"
        f"\t\t\tbuildActionMask = {_PBX_BUILD_ACTION_MASK};\n"
        f"\t\t\tfiles = (\n"
        f"\t\t\t\t{uid['placeholder_build']} /* {test_target}Placeholder.swift in Sources */,\n"
        f"\t\t\t);\n"
        f"\t\t\trunOnlyForDeploymentPostprocessing = 0;\n\t\t}};\n"
        "/* End PBXSourcesBuildPhase section */",
    )

    # 11. PBXTargetDependency
    dep_block = (
        f"\t\t{uid['target_dep']} /* PBXTargetDependency */ = {{\n"
        f"\t\t\tisa = PBXTargetDependency;\n"
        f"\t\t\ttarget = {host_target_uuid} /* {xcode_config.get('scheme', '')} */;\n"
        f"\t\t\ttargetProxy = {uid['container_proxy']} /* PBXContainerItemProxy */;\n"
        f"\t\t}};\n"
    )
    if "/* End PBXTargetDependency section */" in pbx:
        pbx = pbx.replace(
            "/* End PBXTargetDependency section */",
            dep_block + "/* End PBXTargetDependency section */",
        )
    else:
        pbx = pbx.replace(
            "/* Begin XCBuildConfiguration section */",
            "/* Begin PBXTargetDependency section */\n"
            + dep_block
            + "/* End PBXTargetDependency section */\n\n"
            "/* Begin XCBuildConfiguration section */",
        )

    # 12. XCBuildConfiguration (Debug + Release)
    iphoneos_target = xcode_config.get("iphoneos_deployment_target", "14.0")

    dev_team = xcode_config.get("development_team", "")
    if not dev_team:
        m = re.search(r"DEVELOPMENT_TEAM\s*=\s*(\w+)", pbx)
        dev_team = m.group(1) if m else ""

    dev_team_line = f"\t\t\t\tDEVELOPMENT_TEAM = {dev_team};\n" if dev_team else ""

    # Unit tests link against the host app binary; UI tests launch it externally.
    host_link_settings = (
        (
            f'\t\t\t\tBUNDLE_LOADER = "$(TEST_HOST)";\n'
            f'\t\t\t\tTEST_HOST = "$(BUILT_PRODUCTS_DIR)/{app_product_name}.app/{app_product_name}";\n'
        )
        if not is_ui_test
        else (f'\t\t\t\tTEST_TARGET_NAME = "{scheme_name}";\n')
    )

    for cfg_uuid, cfg_name in [
        (uid["config_debug"], "Debug"),
        (uid["config_release"], "Release"),
    ]:
        pbx = pbx.replace(
            "/* End XCBuildConfiguration section */",
            f"\t\t{cfg_uuid} /* {cfg_name} */ = {{\n"
            f"\t\t\tisa = XCBuildConfiguration;\n"
            f"\t\t\tbuildSettings = {{\n"
            f"{host_link_settings}"
            f"\t\t\t\tCODE_SIGN_STYLE = Automatic;\n"
            f"{dev_team_line}"
            f"\t\t\t\tINFOPLIST_FILE = {test_target}/Info.plist;\n"
            f"\t\t\t\tIPHONEOS_DEPLOYMENT_TARGET = {iphoneos_target};\n"
            f"\t\t\t\tLD_RUNPATH_SEARCH_PATHS = (\n"
            f'\t\t\t\t\t"$(inherited)",\n'
            f'\t\t\t\t\t"@executable_path/Frameworks",\n'
            f'\t\t\t\t\t"@loader_path/Frameworks",\n'
            f"\t\t\t\t);\n"
            f"\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = {bundle_id};\n"
            f'\t\t\t\tPRODUCT_NAME = "$(TARGET_NAME)";\n'
            f"\t\t\t\tSWIFT_VERSION = 5.0;\n"
            f'\t\t\t\tTARGETED_DEVICE_FAMILY = "1,2";\n'
            f"\t\t\t}};\n"
            f"\t\t\tname = {cfg_name};\n\t\t}};\n"
            "/* End XCBuildConfiguration section */",
        )

    # 13. XCConfigurationList
    pbx = pbx.replace(
        "/* End XCConfigurationList section */",
        f'\t\t{uid["config_list"]} /* Build configuration list for PBXNativeTarget "{test_target}" */ = {{\n'
        f"\t\t\tisa = XCConfigurationList;\n"
        f"\t\t\tbuildConfigurations = (\n"
        f"\t\t\t\t{uid['config_debug']} /* Debug */,\n"
        f"\t\t\t\t{uid['config_release']} /* Release */,\n"
        f"\t\t\t);\n"
        f"\t\t\tdefaultConfigurationIsVisible = 0;\n"
        f"\t\t\tdefaultConfigurationName = Release;\n\t\t}};\n"
        "/* End XCConfigurationList section */",
    )

    pbxproj_path.write_text(pbx)

    # Update scheme to include test target in TestAction
    scheme_dir = work_dir / project_rel / "xcshareddata" / "xcschemes"
    scheme_path = scheme_dir / (scheme_name + ".xcscheme")
    testable_entry = (
        f"         <TestableReference\n"
        f'            skipped = "NO">\n'
        f"            <BuildableReference\n"
        f'               BuildableIdentifier = "primary"\n'
        f'               BlueprintIdentifier = "{uid["target"]}"\n'
        f'               BuildableName = "{test_target}.xctest"\n'
        f'               BlueprintName = "{test_target}"\n'
        f'               ReferencedContainer = "container:{project_rel.split("/")[-1]}">\n'
        f"            </BuildableReference>\n"
        f"         </TestableReference>\n"
    )
    if scheme_path.exists():
        scheme_xml = scheme_path.read_text()
        if test_target not in scheme_xml:
            if "      <Testables>\n      </Testables>" in scheme_xml:
                scheme_xml = scheme_xml.replace(
                    "      <Testables>\n      </Testables>",
                    f"      <Testables>\n{testable_entry}      </Testables>",
                )
            elif "      </Testables>" in scheme_xml:
                scheme_xml = scheme_xml.replace(
                    "      </Testables>",
                    f"{testable_entry}      </Testables>",
                )
            scheme_path.write_text(scheme_xml)
    else:
        # No saved scheme file (some early commits lack xcshareddata).
        # Create a minimal scheme so xcodebuild finds the injected test target
        # in the TestAction and includes it in the generated xctestrun file.
        scheme_dir.mkdir(parents=True, exist_ok=True)
        proj_container = project_rel.split("/")[-1]
        minimal_scheme = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Scheme\n"
            '   LastUpgradeVersion = "1620"\n'
            '   version = "1.7">\n'
            "   <BuildAction\n"
            '      parallelizeBuildables = "YES"\n'
            '      buildImplicitly = "YES">\n'
            "      <BuildActionEntries>\n"
            "         <BuildActionEntry\n"
            '            buildForTesting = "YES"\n'
            '            buildForRunning = "YES"\n'
            '            buildForProfiling = "YES"\n'
            '            buildForArchiving = "YES"\n'
            '            buildForAnalyzing = "YES">\n'
            "            <BuildableReference\n"
            '               BuildableIdentifier = "primary"\n'
            f'               BlueprintIdentifier = "{host_target_uuid}"\n'
            f'               BuildableName = "{app_product_name}.app"\n'
            f'               BlueprintName = "{scheme_name}"\n'
            f'               ReferencedContainer = "container:{proj_container}">\n'
            "            </BuildableReference>\n"
            "         </BuildActionEntry>\n"
            "      </BuildActionEntries>\n"
            "   </BuildAction>\n"
            "   <TestAction\n"
            '      buildConfiguration = "Debug"\n'
            '      selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB"\n'
            '      selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB"\n'
            '      shouldUseLaunchSchemeArgsEnv = "YES">\n'
            "      <Testables>\n"
            f"{testable_entry}"
            "      </Testables>\n"
            "   </TestAction>\n"
            "   <LaunchAction\n"
            '      buildConfiguration = "Debug"\n'
            '      selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB"\n'
            '      selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB"\n'
            '      launchStyle = "0"\n'
            '      useCustomWorkingDirectory = "NO"\n'
            '      ignoresPersistentStateOnLaunch = "NO"\n'
            '      debugDocumentVersioning = "YES"\n'
            '      debugServiceExtension = "internal"\n'
            '      allowLocationSimulation = "YES">\n'
            "      <BuildableProductRunnable\n"
            '         runnableDebuggingMode = "0">\n'
            "         <BuildableReference\n"
            '            BuildableIdentifier = "primary"\n'
            f'            BlueprintIdentifier = "{host_target_uuid}"\n'
            f'            BuildableName = "{app_product_name}.app"\n'
            f'            BlueprintName = "{scheme_name}"\n'
            f'            ReferencedContainer = "container:{proj_container}">\n'
            "         </BuildableReference>\n"
            "      </BuildableProductRunnable>\n"
            "   </LaunchAction>\n"
            "</Scheme>\n"
        )
        scheme_path.write_text(minimal_scheme)
        logger.info("Created minimal scheme file at %s", scheme_path)

    # Create Info.plist and placeholder test file on disk
    test_dir = work_dir / files_dest
    test_dir.mkdir(parents=True, exist_ok=True)

    info_plist_path = test_dir / "Info.plist"
    if not info_plist_path.exists():
        info_plist_path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            "\t<key>CFBundleDevelopmentRegion</key>\n\t<string>$(DEVELOPMENT_LANGUAGE)</string>\n"
            "\t<key>CFBundleExecutable</key>\n\t<string>$(EXECUTABLE_NAME)</string>\n"
            "\t<key>CFBundleIdentifier</key>\n\t<string>$(PRODUCT_BUNDLE_IDENTIFIER)</string>\n"
            "\t<key>CFBundleInfoDictionaryVersion</key>\n\t<string>6.0</string>\n"
            "\t<key>CFBundleName</key>\n\t<string>$(PRODUCT_NAME)</string>\n"
            "\t<key>CFBundlePackageType</key>\n\t<string>$(PRODUCT_BUNDLE_PACKAGE_TYPE)</string>\n"
            "\t<key>CFBundleShortVersionString</key>\n\t<string>1.0</string>\n"
            "\t<key>CFBundleVersion</key>\n\t<string>1</string>\n"
            "</dict>\n</plist>\n"
        )

    placeholder_path = test_dir / f"{test_target}Placeholder.swift"
    if not placeholder_path.exists():
        if is_ui_test:
            # UI tests access the app through its public interface; @testable import is not allowed.
            placeholder_path.write_text(
                f"import XCTest\n\n"
                f"final class {test_target}Placeholder: XCTestCase {{\n"
                f"    func testPlaceholder() {{ XCTAssertTrue(true) }}\n}}\n"
            )
        else:
            # Unit tests link against the app binary so @testable import works.
            module_name = xcode_config.get(
                "app_test_module", xcode_config.get(XCODE_CONFIG_SCHEME, "")
            )
            host_m = re.search(
                rf"productName\s*=\s*{re.escape(scheme_name)};.*?"
                r"productReference\s*=\s*\w+ /\*\s*([^*]+?\.app)\s*\*/",
                pbx,
                re.DOTALL,
            )
            if host_m:
                product_name = host_m.group(1).strip()[:-4]  # strip .app
                detected = product_name.replace(" ", "_").replace("-", "_")
                if detected:
                    module_name = detected
            placeholder_path.write_text(
                f"import XCTest\n@testable import {module_name}\n\n"
                f"final class {test_target}Placeholder: XCTestCase {{\n"
                f"    func testPlaceholder() {{ XCTAssertTrue(true) }}\n}}\n"
            )

    logger.info(
        "Injected %s target (%s) into %s", test_target, product_type, pbxproj_path
    )
    return True


def inject_app_test_target(xcode_config: dict, work_dir: Path) -> bool:
    """Inject a unit-test target into the Xcode project.

    Thin wrapper around :func:`_inject_test_target` using ``app_test_*`` config
    keys and ``com.apple.product-type.bundle.unit-test``.
    """
    return _inject_test_target(
        xcode_config,
        work_dir,
        test_target=xcode_config.get(XCODE_CONFIG_APP_TEST_TARGET, ""),
        files_dest=xcode_config.get(XCODE_CONFIG_APP_TEST_FILES_DEST, ""),
        project_rel=xcode_config.get(XCODE_CONFIG_PROJECT, ""),
        bundle_id=xcode_config.get("app_test_bundle_id", "com.anvil.tests"),
        scheme_name=xcode_config.get(
            XCODE_CONFIG_APP_TEST_SCHEME, xcode_config.get(XCODE_CONFIG_SCHEME, "")
        ),
        product_type="com.apple.product-type.bundle.unit-test",
        is_ui_test=False,
    )


def inject_ui_test_target(xcode_config: dict, work_dir: Path) -> bool:
    """Inject a UI-test target into the Xcode project.

    Thin wrapper around :func:`_inject_test_target` using ``ui_test_*`` config
    keys and ``com.apple.product-type.bundle.ui-testing``.  UI test bundles
    launch the app externally and do not link against it, so ``BUNDLE_LOADER``
    and ``TEST_HOST`` are omitted from the build settings.
    """
    return _inject_test_target(
        xcode_config,
        work_dir,
        test_target=xcode_config.get(XCODE_CONFIG_UI_TEST_TARGET, ""),
        files_dest=xcode_config.get(XCODE_CONFIG_UI_TEST_FILES_DEST, ""),
        project_rel=xcode_config.get(XCODE_CONFIG_PROJECT, ""),
        bundle_id=xcode_config.get("ui_test_bundle_id", "com.anvil.uitests"),
        scheme_name=xcode_config.get(
            "ui_test_scheme", xcode_config.get(XCODE_CONFIG_SCHEME, "")
        ),
        product_type="com.apple.product-type.bundle.ui-testing",
        is_ui_test=True,
    )


def _build_xcodebuild_app_test_cmd(
    xcode_config: dict,
    work_dir: Path,
    derived_data_dir: Path,
    allow_pkg_resolution: bool = False,
) -> tuple[list[str], Path] | None:
    """Build the xcodebuild test command for **app-level** unit tests.

    Returns ``(cmd, cwd)`` or ``None`` when no app test config is present.

    Unlike :func:`_build_xcodebuild_test_cmd` (SPM package tests), this
    targets the main Xcode project using ``app_test_scheme`` and runs tests
    hosted inside the app bundle.  Uses ad-hoc signing (``CODE_SIGN_IDENTITY=-``)
    so that entitlements (e.g. CloudKit container) are preserved on simulator.
    """
    app_test_scheme = xcode_config.get(XCODE_CONFIG_APP_TEST_SCHEME, "")
    if not app_test_scheme:
        return None

    dest = get_app_test_destination(xcode_config)
    if not dest or "generic/" in dest:
        return None

    cmd = ["xcodebuild", "test"]
    cmd.extend(_resolve_project_args(xcode_config, work_dir))
    cmd.extend(
        [
            "-scheme",
            app_test_scheme,
            "-destination",
            dest,
            "-derivedDataPath",
            str(derived_data_dir),
            *_XCODEBUILD_ADHOC_SIGN_FLAGS,
        ]
    )
    if not allow_pkg_resolution:
        cmd.append("-disableAutomaticPackageResolution")

    app_test_target = xcode_config.get(XCODE_CONFIG_APP_TEST_TARGET, "")
    if app_test_target:
        cmd.extend(["-only-testing", app_test_target])

    # Let xcode_config.yaml exclude pre-existing repo tests that are broken in
    # our CI env (e.g. locale-sensitive formatter tests, snapshot tests missing
    # reference images).  Each entry is passed straight through as
    # `-skip-testing:<value>` (e.g. "MastodonTests/MetricFormatterTests").
    for skip in xcode_config.get("app_test_skip", []):
        cmd.extend(["-skip-testing:" + skip])

    app_test_plan = xcode_config.get("app_test_plan", "")
    if app_test_plan:
        cmd.extend(["-testPlan", app_test_plan])

    cmd.extend(xcode_config.get("extra_build_flags", []))

    return cmd, work_dir


def _run_cmd(
    cmd: list[str], check: bool = True, **kwargs
) -> subprocess.CompletedProcess:
    logger.debug("Running: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
        **kwargs,
    )


def _run_xcodebuild(
    cmd: list[str],
    cwd: str,
    timeout: int,
) -> subprocess.CompletedProcess:
    """Run xcodebuild, killing the entire process group on timeout.

    Each invocation gets a per-cwd ``TMPDIR`` under the system temp root so
    parallel warms of different worktrees don't race on shared tool caches
    (e.g. Sourcery's ``$TMPDIR/SwiftTemplate/<ver>`` build dir, which
    otherwise errors with "item with the same name already exists" under
    concurrency).  Rooting it under ``tempfile.gettempdir()`` (``/var/folders/...``)
    keeps it within the paths that SwiftPM's sandbox allows.
    """
    import tempfile

    logger.debug("Running xcodebuild: %s", " ".join(cmd))
    cwd_hash = hashlib.md5(str(cwd).encode()).hexdigest()[:16]
    tmpdir = Path(tempfile.gettempdir()) / f"anvil-xcb-{cwd_hash}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["TMPDIR"] = str(tmpdir)
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def load_xcode_config(dataset_tasks_dir: Path, dataset_id: str | None = None) -> dict:
    """Load xcode_config.yaml, searching generated and source task dirs."""
    candidates = [dataset_tasks_dir / "xcode_config.yaml"]

    if dataset_id:
        candidates.append(source_tasks_dir(dataset_id) / "xcode_config.yaml")

    for path in candidates:
        if path.exists():
            return _YAML().load(path)

    searched = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"No xcode_config.yaml found. Searched: {searched}. "
        "Create one with project/scheme/destination settings."
    )
