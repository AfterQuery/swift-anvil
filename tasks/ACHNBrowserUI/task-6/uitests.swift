import XCTest

final class AnvilTask6UITests: XCTestCase {

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

    // MARK: - AC 1: Dashboard tab exists in tab bar (existence-only)

    func testDashboardTabExistsInTabBar() {
        guard elementExistsWithin(app.tabBars.firstMatch, timeout: 20) else {
            XCTFail("Tab bar not found"); return
        }
        let dashboardTab = app.tabBars.buttons["Dashboard"]
        let todayTab = app.tabBars.buttons["Today"]
        XCTAssertTrue(
            elementExistsWithin(dashboardTab, timeout: 8) || elementExistsWithin(todayTab, timeout: 5),
            "AC 1: Dashboard/Today tab must appear in the main tab bar"
        )
    }

    // MARK: - AC 1: Dashboard is the default selected tab on launch (existence + property check)

    func testDashboardIsSelectedOnLaunch() {
        guard elementExistsWithin(app.tabBars.firstMatch, timeout: 20) else {
            XCTFail("Tab bar not found"); return
        }
        let dashboardTab = app.tabBars.buttons["Dashboard"]
        let todayTab = app.tabBars.buttons["Today"]
        let tab = elementExistsWithin(dashboardTab) ? dashboardTab : todayTab
        XCTAssertTrue(tab.exists, "Dashboard/Today tab must exist")
        XCTAssertTrue(
            tab.isSelected,
            "AC 1: Dashboard/Today must be the default selected tab when the app launches"
        )
    }

    // MARK: - AC 1: Dashboard navigation bar title

    func testDashboardShowsNavigationBarTitle() {
        guard elementExistsWithin(app.tabBars.firstMatch, timeout: 20) else {
            XCTFail("Tab bar not found"); return
        }
        let dashboardTab = app.tabBars.buttons["Dashboard"]
        let todayTab = app.tabBars.buttons["Today"]
        let tab = elementExistsWithin(dashboardTab) ? dashboardTab : todayTab
        guard tab.exists else { XCTFail("Dashboard/Today tab not found"); return }
        tab.tap()
        let hasDashboardNav = elementExistsWithin(app.navigationBars["Dashboard"], timeout: 8)
        let hasTodayNav = elementExistsWithin(app.navigationBars["Today"], timeout: 5)
        XCTAssertTrue(
            hasDashboardNav || hasTodayNav,
            "AC 1: Dashboard/Today screen must have navigation bar titled 'Dashboard' or 'Today'"
        )
    }

    // MARK: - AC 2/3/4: Dashboard tab appears before Items tab (existence-only)

    func testDashboardTabAppearsBeforeItemsTab() {
        let tabBar = app.tabBars.firstMatch
        guard elementExistsWithin(tabBar, timeout: 20) else {
            XCTFail("Tab bar not found"); return
        }
        let buttons = tabBar.buttons.allElementsBoundByIndex
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
