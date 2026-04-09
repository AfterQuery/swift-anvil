## Fix: Turnip price columns are misaligned across rows

### Problem Description

In the **ACHNBrowserUI** app, the Turnips price prediction section displays daily AM/PM price estimates. Each row in the table independently positions its columns, which causes the AM and PM values to not align vertically across rows. This creates a jagged, hard-to-read layout.

The misalignment affects all three display modes: average prices, min/max prices, and profits.

### Acceptance Criteria

1. The AM and PM columns must be consistently aligned under their headers across all rows.
2. The unified layout must be used for all three display modes (average, min/max, profits).
3. Do not change the chart views, island views, or other unrelated sections of the turnip price screen.

### Required API Surface

The implementation must expose these names (tests depend on them to compile):

- `GridStack` — SwiftUI View; `init(rows:columns:spacing:content:)` where `spacing: CGFloat? = nil` (default is `nil`, not `0`); stored properties `rows: Int`, `columns: Int`, `spacing: CGFloat?`; content closure callable as `content(row, col)`

### Xcode Project Note

This is a traditional Xcode project (not SwiftPM). When you add or remove `.swift` files, you must also update `project.pbxproj` to register/unregister them in the build target.
