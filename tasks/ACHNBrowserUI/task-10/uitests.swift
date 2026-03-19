import XCTest

final class AnvilTask10UITests: XCTestCase {

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

    /// Poll for element existence (avoids waitForExistence blocking ~60s on cold simulators).
    private func elementExistsWithin(_ element: XCUIElement, timeout: TimeInterval = 15) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if element.exists { return true }
            Thread.sleep(forTimeInterval: 1)
        }
        return false
    }

    /// Poll for "More" button (Collection view can load slowly on cold launch).
    /// Tabs.more rawValue is "more"; display may be "More" or "more" depending on localization.
    private func moreButtonExists(timeout: TimeInterval = 90) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if app.buttons["More"].exists || app.buttons["more"].exists { return true }
            Thread.sleep(forTimeInterval: 1)
        }
        return false
    }

    private func tapMoreSegment() {
        if app.buttons["More"].exists { app.buttons["More"].tap(); return }
        if app.buttons["more"].exists { app.buttons["more"].tap(); return }
        XCTFail("More segment not found")
    }

    /// Tab bar visible (app ready). Use before tab bar checks on cold launch.
    private func waitForAppReady(timeout: TimeInterval = 20) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if app.tabBars.firstMatch.exists { return true }
            Thread.sleep(forTimeInterval: 0.5)
        }
        return false
    }

    // MARK: - AC 4/5: "More" option exists in Collection (existence-only after minimal navigation)

    func testMoreOptionExistsInCollection() {
        XCTAssertTrue(waitForAppReady(), "App tab bar must appear")
        let collectionTab = app.tabBars.buttons["Collection"]
        guard elementExistsWithin(collectionTab, timeout: 10) else {
            XCTFail("Collection tab not found in tab bar"); return
        }
        collectionTab.tap()
        XCTAssertTrue(
            moreButtonExists(timeout: 90),
            "AC 4/5: A 'More' option must exist in the Collection picker after the patch"
        )
    }
    
    // MARK: - AC 8: Existing Items tab still accessible (existence-only)

    func testItemsOrCatalogTabStillExists() {
        XCTAssertTrue(waitForAppReady(), "App tab bar must appear")
        var found = false
        for label in ["Items", "Catalog", "Collection"] {
            if elementExistsWithin(app.tabBars.buttons[label], timeout: 5) {
                found = true
                break
            }
        }
        XCTAssertTrue(found, "AC 8: Items/Catalog tab must remain accessible after the patch")
    }

    // MARK: - AC 8: Villagers still accessible (existence-only)

    func testVillagersTabStillExists() {
        XCTAssertTrue(waitForAppReady(), "App tab bar must appear")
        XCTAssertTrue(
            elementExistsWithin(app.tabBars.buttons["Villagers"], timeout: 10),
            "AC 8: Villagers tab must remain after Collection reorganisation"
        )
    }

    // MARK: - AC 9: App launches without regression (existence-only)

    func testAppLaunchesWithoutCrash() {
        XCTAssertTrue(app.exists, "AC 9: App must launch without crashing after the patch")
    }
}
