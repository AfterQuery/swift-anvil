import XCTest

final class AnvilTask7UITests: XCTestCase {

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

    // MARK: - AC 1: Turnips tab exists in the main tab bar (existence-only)

    func testTurnipsTabExistsInTabBar() {
        XCTAssertTrue(
            app.tabBars.buttons["Turnips"].waitForExistence(timeout: 15),
            "AC 1: Turnips tab must exist in the main tab bar"
        )
    }

    // MARK: - AC 1/2: Tapping Turnips shows TurnipsView

    func testTurnipsTabShowsNavigationBarTitle() {
        let tab = app.tabBars.buttons["Turnips"]
        guard tab.waitForExistence(timeout: 15) else { XCTFail("Turnips tab not found"); return }
        tab.tap()
        let hasNavBar = app.navigationBars["Turnips"].waitForExistence(timeout: 8)
        let hasContent = app.staticTexts["Open Islands"].waitForExistence(timeout: 8) ||
                         app.staticTexts["Chart"].waitForExistence(timeout: 5)
        XCTAssertTrue(
            hasNavBar || hasContent,
            "AC 1: Tapping Turnips tab must show TurnipsView content"
        )
    }

    // MARK: - AC 3: Catalog (previously Items) tab uses drill-down CategoriesView (existence-only)

    func testCatalogOrItemsTabStillExists() {
        XCTAssertTrue(
            app.tabBars.buttons["Catalog"].waitForExistence(timeout: 10) ||
            app.tabBars.buttons["Items"].waitForExistence(timeout: 8),
            "AC 3: A catalog/items tab ('Catalog' or 'Items') must exist"
        )
    }

    // MARK: - AC 3: Tapping Catalog shows a list (drill-down navigation)

    func testCatalogTabShowsDrillDownList() {
        let catalogTab = app.tabBars.buttons["Catalog"]
        let tab = catalogTab.waitForExistence(timeout: 5) ? catalogTab : app.tabBars.buttons["Items"]
        guard tab.waitForExistence(timeout: 8) else { XCTFail("Catalog/Items tab not found"); return }
        tab.tap()
        XCTAssertTrue(
            app.navigationBars.firstMatch.waitForExistence(timeout: 8),
            "AC 3: Catalog tab must display a navigation-based drill-down list"
        )
    }

    // MARK: - AC 6: Villagers tab is unchanged (existence-only)

    func testVillagersTabStillExists() {
        XCTAssertTrue(
            app.tabBars.buttons["Villagers"].waitForExistence(timeout: 15),
            "AC 6: Villagers tab must still exist after adding Turnips tab"
        )
    }

    // MARK: - AC 7: App launches without regression (existence-only)

    func testAppLaunchesWithoutCrash() {
        XCTAssertTrue(app.exists, "AC 7: App must launch without crashing after the patch")
    }
}
