import XCTest

final class AnvilTask6UITests: XCTestCase {

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

    private var dashboardTab: XCUIElement {
        let tab = app.tabBars.buttons["Dashboard"]
        return tab.exists ? tab : app.tabBars.buttons["Today"]
    }

    // MARK: - AC 1: Dashboard tab exists in tab bar (existence-only)

    func testDashboardTabExistsInTabBar() {
        XCTAssertTrue(
            app.tabBars.buttons["Dashboard"].waitForExistence(timeout: 8) ||
            app.tabBars.buttons["Today"].waitForExistence(timeout: 5),
            "AC 1: Dashboard/Today tab must appear in the main tab bar"
        )
    }

    // MARK: - AC 1: Dashboard is the default selected tab on launch (existence + property check)

    func testDashboardIsSelectedOnLaunch() {
        let tab = dashboardTab
        XCTAssertTrue(tab.exists, "Dashboard/Today tab must exist")
        XCTAssertTrue(
            tab.isSelected,
            "AC 1: Dashboard/Today must be the default selected tab when the app launches"
        )
    }

    // MARK: - AC 1: Dashboard navigation bar title

    func testDashboardShowsNavigationBarTitle() {
        let tab = dashboardTab
        guard tab.exists else { XCTFail("Dashboard/Today tab not found"); return }
        tab.tap()
        XCTAssertTrue(
            app.navigationBars["Dashboard"].waitForExistence(timeout: 8) ||
            app.navigationBars["Today"].waitForExistence(timeout: 5),
            "AC 1: Dashboard/Today screen must have navigation bar titled 'Dashboard' or 'Today'"
        )
    }

    // MARK: - AC 2/3/4: Dashboard tab appears before Items tab (existence-only)

    func testDashboardTabAppearsBeforeItemsTab() {
        let buttons = app.tabBars.firstMatch.buttons.allElementsBoundByIndex
        guard !buttons.isEmpty else { XCTFail("No tab bar buttons found"); return }
        let firstLabel = buttons.first?.label ?? ""
        let isDashboard = firstLabel.lowercased().contains("dashboard") || firstLabel.lowercased() == "today"
        XCTAssertTrue(
            isDashboard,
            "AC 1: Dashboard/Today must be the first tab in the tab bar (got: \(firstLabel))"
        )
    }

    // MARK: - AC 4: App launches and renders without crash (existence-only)

    func testAppLaunchesWithoutCrash() {
        XCTAssertTrue(app.exists, "AC 4: App must launch without crashing after adding Dashboard")
    }
}
