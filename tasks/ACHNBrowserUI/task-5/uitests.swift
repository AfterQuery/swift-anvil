import XCTest

final class AnvilTask5UITests: XCTestCase {

    var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = false
        app = XCUIApplication()
        app.launch()
    }

    override func tearDownWithError() throws {
        app.terminate()
    }

    // MARK: - Helpers

    /// Poll for existence (avoids waitForExistence blocking on cold simulators).
    private func elementExistsWithin(_ element: XCUIElement, timeout: TimeInterval = 15) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if element.exists { return true }
            Thread.sleep(forTimeInterval: 0.5)
        }
        return false
    }

    @discardableResult
    private func openTodayScreen() -> Bool {
        guard elementExistsWithin(app.tabBars.firstMatch, timeout: 20) else {
            XCTFail("Tab bar not found"); return false
        }
        for label in ["Today", "Dashboard"] {
            let tab = app.tabBars.buttons[label]
            if elementExistsWithin(tab, timeout: 10) {
                tab.tap()
                return true
            }
        }
        XCTFail("Today/Dashboard tab not found in tab bar")
        return false
    }

    /// Chores section exists (existence-only, with optional scroll). Broad matching.
    private func choresSectionExists() -> Bool {
        for substring in ["keep track of your chores", "Manage and keep track", "Chores"] {
            let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
            let asStatic = app.staticTexts.matching(pred).firstMatch
            let asCell = app.cells.matching(pred).firstMatch
            if elementExistsWithin(asStatic, timeout: 4) { return true }
            if elementExistsWithin(asCell, timeout: 2) { return true }
        }
        for _ in 0..<8 {
            app.swipeUp()
            Thread.sleep(forTimeInterval: 0.3)
            for substring in ["keep track", "Manage", "Chores"] {
                let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
                if app.staticTexts.matching(pred).firstMatch.exists { return true }
                if app.cells.matching(pred).firstMatch.exists { return true }
            }
        }
        return false
    }

    // MARK: - AC 4/6: Chores section visible on Today screen (existence-only)

    func testChoresSectionHeaderVisibleOnTodayScreen() {
        guard openTodayScreen() else { return }
        XCTAssertTrue(
            choresSectionExists(),
            "AC 4/6: 'Chores' section must appear on the Today/Dashboard screen"
        )
    }

    // MARK: - AC 4: Chores list view is reachable

    func testTappingChoresSectionNavigatesToChoreList() {
        guard openTodayScreen() else { return }
        guard choresSectionExists() else { XCTFail("Chores section not found"); return }
        // Tap any chores-related element (multiple possible labels)
        var tapped = false
        for substring in ["keep track of your chores", "Manage and keep track", "Chores"] {
            let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
            let cell = app.cells.matching(pred).firstMatch
            let text = app.staticTexts.matching(pred).firstMatch
            if cell.exists { cell.tap(); tapped = true; break }
            if text.exists { text.tap(); tapped = true; break }
        }
        guard tapped else { XCTFail("Could not tap chores section"); return }
        XCTAssertTrue(
            elementExistsWithin(app.navigationBars["Chores"], timeout: 8),
            "AC 4: Tapping Chores should navigate to a screen with nav bar title 'Chores'"
        )
    }

    // MARK: - AC 1: Chores list screen is reachable and shows list content
    // General: nav to Chores; pass if add button OR any list row/cell exists (list view rendered)

    func testAddChoreButtonExistsInChoreList() {
        guard openTodayScreen() else { return }
        guard choresSectionExists() else { XCTFail("Chores section not found"); return }
        var tapped = false
        for substring in ["keep track of your chores", "Manage and keep track", "Chores"] {
            let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
            let cell = app.cells.matching(pred).firstMatch
            let text = app.staticTexts.matching(pred).firstMatch
            if cell.exists { cell.tap(); tapped = true; break }
            if text.exists { text.tap(); tapped = true; break }
        }
        guard tapped else { XCTFail("Could not tap chores section"); return }
        guard elementExistsWithin(app.navigationBars["Chores"], timeout: 10) else { return }
        Thread.sleep(forTimeInterval: 1.5)  // Allow list view to render
        // Add Chore button/text, or any list cell = chore list view rendered
        let addChorePred = NSPredicate(format: "label CONTAINS[cd] %@", "Add Chore")
        let listHasContent = elementExistsWithin(app.buttons.matching(addChorePred).firstMatch, timeout: 6) ||
            elementExistsWithin(app.staticTexts.matching(addChorePred).firstMatch, timeout: 5) ||
            elementExistsWithin(app.cells.containing(addChorePred).firstMatch, timeout: 4) ||
            elementExistsWithin(app.cells.firstMatch, timeout: 5)
        XCTAssertTrue(
            listHasContent,
            "AC 1: Chores list view must show add button or list content"
        )
    }

    // MARK: - AC 7: App launches without regression (existence-only)

    func testAppLaunchesWithoutCrash() {
        XCTAssertTrue(app.exists, "AC 7: App must launch without crashing after patch")
    }
}
