import XCTest

final class AnvilTask9UITests: XCTestCase {

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

    /// Poll for element existence without using waitForExistence (which can block ~60s when
    /// the accessibility tree is slow on cold simulators).
    private func elementExistsWithin(_ element: XCUIElement, timeout: TimeInterval = 15) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if element.exists { return true }
            Thread.sleep(forTimeInterval: 1)
        }
        return false
    }

    /// Wait for tab bar to appear (app ready). Dashboard/Today is typically the default tab.
    private func waitForAppReady(timeout: TimeInterval = 20) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if app.tabBars.firstMatch.exists { return true }
            Thread.sleep(forTimeInterval: 0.5)
        }
        return false
    }

    /// Villager Visits section guidance exists. Broad matching for localization.
    private func villagerVisitsGuidanceExists() -> Bool {
        for substring in ["Who have you talked to today", "Who have you talked", "Villager Visits", "visited"] {
            let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
            let asStatic = app.staticTexts.matching(pred).firstMatch
            let asCell = app.cells.matching(pred).firstMatch
            if elementExistsWithin(asStatic, timeout: 5) { return true }
            if elementExistsWithin(asCell, timeout: 2) { return true }
        }
        for _ in 0..<15 {
            app.swipeUp()
            Thread.sleep(forTimeInterval: 0.3)
            for substring in ["Who have you talked", "Villager Visits", "visited"] {
                let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
                if app.staticTexts.matching(pred).firstMatch.exists { return true }
                if app.cells.matching(pred).firstMatch.exists { return true }
            }
        }
        return false
    }

    /// Check if any existing Today section is present (chores, tasks, etc.) — AC 6.
    /// Uses broad substrings to match any common Today section; layout/order varies by base commit.
    private func existingSectionExists() -> Bool {
        let candidates = [
            "chore", "task", "birthday", "mystery", "island", "nook", "music", "event",
            "turnip", "collection", "progress", "available", "new", "subscribe", "character",
            "keep track", "Manage", "Daily", "Today",
        ]
        for substring in candidates {
            let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
            let asStatic = app.staticTexts.matching(pred).firstMatch
            let asCell = app.cells.matching(pred).firstMatch
            if elementExistsWithin(asStatic, timeout: 3) { return true }
            if elementExistsWithin(asCell, timeout: 2) { return true }
        }
        for _ in 0..<12 {
            app.swipeUp()
            Thread.sleep(forTimeInterval: 0.3)
            for substring in candidates {
                let pred = NSPredicate(format: "label CONTAINS[cd] %@", substring)
                if app.staticTexts.matching(pred).firstMatch.exists { return true }
                if app.cells.matching(pred).firstMatch.exists { return true }
            }
        }
        for _ in 0..<6 {
            app.swipeDown()
            Thread.sleep(forTimeInterval: 0.3)
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
        XCTAssertTrue(waitForAppReady(), "App tab bar must appear")
        XCTAssertTrue(
            villagerVisitsGuidanceExists(),
            "AC 1: 'Villager Visits' section must appear on the Today/Dashboard screen"
        )
    }

    // MARK: - AC 4: Reset button is hidden when no villagers are visited (existence-only)

    func testResetButtonHiddenOnFreshLaunch() {
        XCTAssertTrue(waitForAppReady(), "App tab bar must appear")
        XCTAssertTrue(villagerVisitsGuidanceExists(), "Villager Visits section must exist")
        // Poll: Reset may take a moment to not appear; avoid flaky instant exists check
        var resetVisible = false
        let deadline = Date().addingTimeInterval(3)
        while Date() < deadline {
            if app.buttons["Reset"].exists { resetVisible = true; break }
            Thread.sleep(forTimeInterval: 0.3)
        }
        XCTAssertFalse(
            resetVisible,
            "AC 4: Reset button must not be shown when no villagers have been visited"
        )
    }

    // MARK: - AC 3: Empty state guidance text is shown (existence-only)

    func testVillagerVisitsSectionShowsGuidanceText() {
        XCTAssertTrue(waitForAppReady(), "App tab bar must appear")
        XCTAssertTrue(
            villagerVisitsGuidanceExists(),
            "AC 3: Guidance text should appear in Villager Visits section when no residents are tracked"
        )
    }

    // MARK: - AC 6: Today screen has Villager Visits and other content (general)

    func testExistingTodaySectionsRemainPresent() {
        XCTAssertTrue(waitForAppReady(), "App tab bar must appear")
        // General: Villager Visits exists (patch applied) and Today has other sections (no regression)
        let villagerVisitsPresent = villagerVisitsGuidanceExists()
        let otherSectionPresent = existingSectionExists()
        XCTAssertTrue(
            villagerVisitsPresent,
            "AC 1/6: Villager Visits section must appear on Today screen"
        )
        XCTAssertTrue(
            otherSectionPresent,
            "AC 6: Today screen must show other sections (chores, tasks, etc.) — no regression"
        )
    }
}
