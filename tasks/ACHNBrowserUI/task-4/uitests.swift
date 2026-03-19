import XCTest

final class AnvilTask4UITests: XCTestCase {

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
    private func openVillagersTab() -> Bool {
        let tab = app.tabBars.buttons["Villagers"]
        guard tab.waitForExistence(timeout: 10) else {
            XCTFail("Villagers tab not found in tab bar"); return false
        }
        tab.tap()
        return true
    }

    private func openSortActionSheet() {
        let sortBtn = app.navigationBars.buttons["arrow.up.arrow.down.circle"]
        XCTAssertTrue(sortBtn.waitForExistence(timeout: 8), "Sort button not found")
        sortBtn.tap()
    }

    private func dismissActionSheetIfPresent() {
        let cancelBtn = app.buttons.matching(NSPredicate(format: "label CONTAINS[cd] 'Cancel'")).firstMatch
        if cancelBtn.waitForExistence(timeout: 2) {
            cancelBtn.tap()
        } else {
            app.swipeDown()
        }
    }

    // MARK: - AC 3: Sort button in navigation bar

    func testSortButtonExistsInVillagersNavigationBar() {
        guard openVillagersTab() else { return }
        XCTAssertTrue(
            app.navigationBars.buttons["arrow.up.arrow.down.circle"].waitForExistence(timeout: 8),
            "AC 3: Sort button (arrow.up.arrow.down.circle) must appear in Villagers nav bar"
        )
    }

    // MARK: - AC 3/4: Sort icon changes when sort is active

    func testSortButtonIconChangesWhenSortIsActive() {
        guard openVillagersTab() else { return }
        openSortActionSheet()
        let nameOption = app.buttons["Name"]
        XCTAssertTrue(nameOption.waitForExistence(timeout: 5), "Name sort option not found in action sheet")
        nameOption.tap()
        XCTAssertTrue(
            app.navigationBars.buttons["arrow.up.arrow.down.circle.fill"].waitForExistence(timeout: 5),
            "AC 4: Sort button icon must switch to filled variant when a sort is active"
        )
    }

    // MARK: - AC 1/2: Sort options in action sheet

    func testTappingSortButtonPresentsSortByNameOption() {
        guard openVillagersTab() else { return }
        openSortActionSheet()
        XCTAssertTrue(
            app.buttons["Name"].waitForExistence(timeout: 5),
            "AC 1: 'Name' sort option must appear in the sort action sheet"
        )
    }

    func testTappingSortButtonPresentsSortBySpeciesOption() {
        guard openVillagersTab() else { return }
        openSortActionSheet()
        XCTAssertTrue(
            app.buttons["Species"].waitForExistence(timeout: 5),
            "AC 1: 'Species' sort option must appear in the sort action sheet"
        )
        dismissActionSheetIfPresent()
    }

    // MARK: - AC 6: Clear selection option

    func testSortActionSheetContainsClearSelectionOption() {
        guard openVillagersTab() else { return }
        openSortActionSheet()
        let nameOption = app.buttons["Name"]
        XCTAssertTrue(nameOption.waitForExistence(timeout: 5), "Name sort option not found")
        nameOption.tap()
        let activeButton = app.navigationBars.buttons["arrow.up.arrow.down.circle.fill"]
        XCTAssertTrue(activeButton.waitForExistence(timeout: 5), "Active sort button not found after selecting Name")
        activeButton.tap()
        XCTAssertTrue(
            app.buttons["Clear Selection"].waitForExistence(timeout: 5),
            "AC 6: 'Clear Selection' option must appear in the sort action sheet when a sort is active"
        )
        dismissActionSheetIfPresent()
    }
}
