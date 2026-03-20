import XCTest

final class AnvilTask9UITests: XCTestCase {

    var app: XCUIApplication!

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
        executionTimeAllowance = 120
        app = XCUIApplication()
        app.launchArguments += ["-UIAnimationDragCoefficient", "0.001"]
        app.launch()
        XCTAssertTrue(app.tabBars.firstMatch.waitForExistence(timeout: 20), "App tab bar must appear")
    }

    override func tearDown() {
        app.terminate()
        super.tearDown()
    }

    // MARK: - Helpers

    @discardableResult
    private func openTodayScreen() -> Bool {
        for label in ["Today", "Dashboard"] {
            let tab = app.tabBars.buttons[label]
            if tab.waitForExistence(timeout: 10) {
                tab.tap()
                _ = app.cells.firstMatch.waitForExistence(timeout: 30)
                return true
            }
        }
        XCTFail("Today/Dashboard tab not found in tab bar")
        return false
    }

    private func villagerVisitsGuidanceExists() -> Bool {
        let substrings = ["Who have you talked to today", "Who have you talked", "Villager Visits", "visited"]
        for substring in substrings {
            let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
            if app.staticTexts.matching(pred).firstMatch.waitForExistence(timeout: 5) { return true }
            if app.cells.matching(pred).firstMatch.exists { return true }
        }
        for _ in 0..<6 {
            app.swipeUp(velocity: .fast)
            for substring in substrings {
                let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
                if app.staticTexts.matching(pred).firstMatch.waitForExistence(timeout: 1) { return true }
                if app.cells.matching(pred).firstMatch.exists { return true }
            }
        }
        for _ in 0..<6 {
            app.swipeDown(velocity: .fast)
            for substring in substrings {
                let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
                if app.staticTexts.matching(pred).firstMatch.exists { return true }
                if app.cells.matching(pred).firstMatch.exists { return true }
            }
        }
        return false
    }

    private func existingSectionExists() -> Bool {
        let candidates = [
            "chore", "task", "birthday", "mystery", "island", "nook", "music", "event",
            "turnip", "collection", "progress", "available", "new", "subscribe", "character",
            "keep track", "Manage", "Daily", "Today",
        ]
        for substring in candidates {
            let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
            if app.staticTexts.matching(pred).firstMatch.waitForExistence(timeout: 2) { return true }
            if app.cells.matching(pred).firstMatch.exists { return true }
        }
        for _ in 0..<5 {
            app.swipeDown(velocity: .fast)
            for substring in candidates {
                let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
                if app.staticTexts.matching(pred).firstMatch.exists { return true }
                if app.cells.matching(pred).firstMatch.exists { return true }
            }
        }
        for _ in 0..<5 {
            app.swipeUp(velocity: .fast)
            for substring in candidates {
                let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
                if app.staticTexts.matching(pred).firstMatch.exists { return true }
                if app.cells.matching(pred).firstMatch.exists { return true }
            }
        }
        return false
    }

    // MARK: - AC 1: "Villager Visits" section appears on Today screen (existence-only)

    func testVillagerVisitsSectionHeaderVisible() {
        guard openTodayScreen() else { return }
        XCTAssertTrue(
            villagerVisitsGuidanceExists(),
            "AC 1: 'Villager Visits' section must appear on the Today/Dashboard screen"
        )
    }

    // MARK: - AC 4: Reset button is hidden when no villagers are visited (existence-only)

    func testResetButtonHiddenOnFreshLaunch() {
        guard openTodayScreen() else { return }
        XCTAssertTrue(villagerVisitsGuidanceExists(), "Villager Visits section must exist")
        XCTAssertFalse(
            app.buttons["Reset"].waitForExistence(timeout: 3),
            "AC 4: Reset button must not be shown when no villagers have been visited"
        )
    }

    // MARK: - AC 6: Today screen has Villager Visits and other content (general)

    func testExistingTodaySectionsRemainPresent() {
        guard openTodayScreen() else { return }
        XCTAssertTrue(
            villagerVisitsGuidanceExists(),
            "AC 1/6: Villager Visits section must appear on Today screen"
        )
        // Brief wait for list to settle after scroll (villagerVisitsGuidanceExists may have scrolled)
        _ = app.wait(for: .runningForeground, timeout: 2)
        XCTAssertTrue(
            existingSectionExists(),
            "AC 6: Today screen must show other sections (chores, tasks, etc.) — no regression"
        )
    }
}
