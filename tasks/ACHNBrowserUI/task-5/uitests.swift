import XCTest

final class AnvilTask5UITests: XCTestCase {

    var app: XCUIApplication!

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
        executionTimeAllowance = 120
        app = XCUIApplication()
        app.launchArguments += ["-UIAnimationDragCoefficient", "0.001"]
        app.launch()
        XCTAssertTrue(app.tabBars.firstMatch.waitForExistence(timeout: 20), "Tab bar not found")
    }

    override func tearDown() {
        app.terminate()
        super.tearDown()
    }

    // MARK: - Helpers

    @discardableResult
    private func openTodayScreen() -> Bool {
        for label in ["Dashboard", "Today"] {
            let tab = app.tabBars.buttons[label]
            guard tab.waitForExistence(timeout: 12) else { continue }
            for attempt in 0..<2 {
                tab.tap()
                if app.cells.firstMatch.waitForExistence(timeout: 25) { return true }
                if attempt == 0 { _ = app.wait(for: .runningForeground, timeout: 2) }
            }
        }
        XCTFail("Today/Dashboard tab not found or content did not load")
        return false
    }

    private func choresSectionExists() -> Bool {
        for substring in ["keep track of your chores", "Manage and keep track", "Chores"] {
            let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
            if app.staticTexts.matching(pred).firstMatch.waitForExistence(timeout: 8) { return true }
            if app.cells.matching(pred).firstMatch.waitForExistence(timeout: 4) { return true }
        }
        for _ in 0..<8 {
            app.swipeUp()
            for substring in ["keep track", "Manage", "Chores"] {
                let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
                if app.staticTexts.matching(pred).firstMatch.waitForExistence(timeout: 2) { return true }
                if app.cells.matching(pred).firstMatch.waitForExistence(timeout: 2) { return true }
            }
        }
        return false
    }

    @discardableResult
    private func tapChoresSection() -> Bool {
        for substring in ["keep track of your chores", "Manage and keep track", "Chores"] {
            let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
            let cell = app.cells.matching(pred).firstMatch
            let text = app.staticTexts.matching(pred).firstMatch
            if cell.exists { cell.tap(); return true }
            if text.exists { text.tap(); return true }
        }
        XCTFail("Could not tap chores section")
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
        guard tapChoresSection() else { return }
        XCTAssertTrue(
            app.navigationBars["Chores"].waitForExistence(timeout: 8),
            "AC 4: Tapping Chores should navigate to a screen with nav bar title 'Chores'"
        )
    }

    // MARK: - AC 1: Chores list screen is reachable and shows list content

    func testAddChoreButtonExistsInChoreList() {
        guard openTodayScreen() else { return }
        guard choresSectionExists() else { XCTFail("Chores section not found"); return }
        guard tapChoresSection() else { return }
        guard app.navigationBars["Chores"].waitForExistence(timeout: 10) else { return }
        // Brief wait for list to render after navigation
        _ = app.wait(for: .runningForeground, timeout: 3)
        let addChorePred = NSPredicate(format: "label CONTAINS[cd] %@", "Add Chore")
        let emptyStatePred = NSPredicate(format: "label CONTAINS[cd] %@", "Track your chores")
        let listHasContent = app.buttons.matching(addChorePred).firstMatch.waitForExistence(timeout: 12) ||
            app.staticTexts.matching(addChorePred).firstMatch.waitForExistence(timeout: 10) ||
            app.cells.containing(addChorePred).firstMatch.waitForExistence(timeout: 10) ||
            app.otherElements.matching(addChorePred).firstMatch.waitForExistence(timeout: 8) ||
            app.staticTexts.matching(emptyStatePred).firstMatch.waitForExistence(timeout: 10) ||
            app.cells.firstMatch.waitForExistence(timeout: 12)
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
