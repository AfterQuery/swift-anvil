import XCTest

final class AnvilTask7UITests: XCTestCase {

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

    /// Tab bar visible (app ready). Use before tab bar checks on cold launch.
    private func waitForAppReady(timeout: TimeInterval = 20) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if app.tabBars.firstMatch.exists { return true }
            Thread.sleep(forTimeInterval: 0.5)
        }
        return false
    }

    // MARK: - AC 1: Turnips tab exists in the main tab bar (existence-only)

    func testTurnipsTabExistsInTabBar() {
        XCTAssertTrue(waitForAppReady(), "App tab bar must appear")
        XCTAssertTrue(
            elementExistsWithin(app.tabBars.buttons["Turnips"]),
            "AC 1: Turnips tab must exist in the main tab bar"
        )
    }

    // MARK: - AC 1/2: Tapping Turnips shows TurnipsView

    func testTurnipsTabShowsNavigationBarTitle() {
        XCTAssertTrue(waitForAppReady(), "App tab bar must appear")
        let tab = app.tabBars.buttons["Turnips"]
        guard elementExistsWithin(tab) else { XCTFail("Turnips tab not found"); return }
        tab.tap()
        let hasNavBar = elementExistsWithin(app.navigationBars["Turnips"], timeout: 8)
        let hasContent = elementExistsWithin(app.staticTexts["Open Islands"], timeout: 8) ||
                         elementExistsWithin(app.staticTexts["Chart"], timeout: 5)
        XCTAssertTrue(
            hasNavBar || hasContent,
            "AC 1: Tapping Turnips tab must show TurnipsView content"
        )
    }

    // MARK: - AC 3: Catalog (previously Items) tab uses drill-down CategoriesView (existence-only)

    func testCatalogOrItemsTabStillExists() {
        XCTAssertTrue(waitForAppReady(), "App tab bar must appear")
        let hasCatalog = elementExistsWithin(app.tabBars.buttons["Catalog"], timeout: 10)
        let hasItems = elementExistsWithin(app.tabBars.buttons["Items"], timeout: 8)
        XCTAssertTrue(
            hasCatalog || hasItems,
            "AC 3: A catalog/items tab ('Catalog' or 'Items') must exist"
        )
    }

    // MARK: - AC 3: Tapping Catalog shows a list (drill-down navigation)

    func testCatalogTabShowsDrillDownList() {
        XCTAssertTrue(waitForAppReady(), "App tab bar must appear")
        let catalogTab = app.tabBars.buttons["Catalog"]
        let itemsTab = app.tabBars.buttons["Items"]
        let tab = elementExistsWithin(catalogTab, timeout: 5) ? catalogTab : itemsTab
        guard elementExistsWithin(tab) else { XCTFail("Catalog/Items tab not found"); return }
        tab.tap()
        Thread.sleep(forTimeInterval: 0.5)  // Allow navigation to settle
        XCTAssertTrue(
            elementExistsWithin(app.navigationBars.firstMatch, timeout: 8),
            "AC 3: Catalog tab must display a navigation-based drill-down list"
        )
    }

    // MARK: - AC 6: Villagers tab is unchanged (existence-only)

    func testVillagersTabStillExists() {
        XCTAssertTrue(waitForAppReady(), "App tab bar must appear")
        XCTAssertTrue(
            elementExistsWithin(app.tabBars.buttons["Villagers"]),
            "AC 6: Villagers tab must still exist after adding Turnips tab"
        )
    }

    // MARK: - AC 7: App launches without regression (existence-only)

    func testAppLaunchesWithoutCrash() {
        XCTAssertTrue(app.exists, "AC 7: App must launch without crashing after the patch")
    }
}
