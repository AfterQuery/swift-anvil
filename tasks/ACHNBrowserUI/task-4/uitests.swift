import XCTest

final class AnvilTask4UITests: XCTestCase {

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

    /// Dismiss action sheet (Cancel or swipe down). Tolerates localization.
    private func dismissActionSheetIfPresent() {
        let cancelPred = NSPredicate(format: "label CONTAINS[cd] %@", "Cancel")
        let cancelBtn = app.buttons.matching(cancelPred).firstMatch
        if elementExistsWithin(cancelBtn, timeout: 2) {
            cancelBtn.tap()
        } else {
            app.swipeDown()
        }
    }

    @discardableResult
    private func openVillagersTab() -> Bool {
        guard elementExistsWithin(app.tabBars.firstMatch, timeout: 20) else {
            XCTFail("Tab bar not found"); return false
        }
        let tab = app.tabBars.buttons["Villagers"]
        guard elementExistsWithin(tab, timeout: 10) else {
            XCTFail("Villagers tab not found in tab bar")
            return false
        }
        tab.tap()
        Thread.sleep(forTimeInterval: 0.5)  // Allow view to settle
        return true
    }

    // MARK: - AC 3: Sort button in navigation bar

    func testSortButtonExistsInVillagersNavigationBar() {
        guard openVillagersTab() else { return }
        let sortBtn = app.navigationBars.buttons["arrow.up.arrow.down.circle"]
        XCTAssertTrue(
            elementExistsWithin(sortBtn, timeout: 8),
            "AC 3: Sort button (arrow.up.arrow.down.circle) must appear in Villagers nav bar"
        )
    }

    // MARK: - AC 3/4: Sort icon changes when sort is active

    func testSortButtonIconChangesWhenSortIsActive() {
        guard openVillagersTab() else { return }
        let inactiveButton = app.navigationBars.buttons["arrow.up.arrow.down.circle"]
        guard elementExistsWithin(inactiveButton, timeout: 8) else {
            XCTFail("Sort button not found"); return
        }
        inactiveButton.tap()

        let nameOption = app.buttons["Name"]
        guard elementExistsWithin(nameOption, timeout: 5) else {
            XCTFail("Name sort option not found in action sheet"); return
        }
        nameOption.tap()

        let filledBtn = app.navigationBars.buttons["arrow.up.arrow.down.circle.fill"]
        XCTAssertTrue(
            elementExistsWithin(filledBtn, timeout: 5),
            "AC 4: Sort button icon must switch to filled variant when a sort is active"
        )
    }

    // MARK: - AC 1/2: Sort options in action sheet

    func testTappingSortButtonPresentsSortByNameOption() {
        guard openVillagersTab() else { return }
        let sortButton = app.navigationBars.buttons["arrow.up.arrow.down.circle"]
        guard elementExistsWithin(sortButton, timeout: 8) else {
            XCTFail("Sort button not found"); return
        }
        sortButton.tap()

        XCTAssertTrue(
            elementExistsWithin(app.buttons["Name"], timeout: 5),
            "AC 1: 'Name' sort option must appear in the sort action sheet"
        )
    }

    func testTappingSortButtonPresentsSortBySpeciesOption() {
        guard openVillagersTab() else { return }
        let sortButton = app.navigationBars.buttons["arrow.up.arrow.down.circle"]
        guard elementExistsWithin(sortButton, timeout: 8) else {
            XCTFail("Sort button not found"); return
        }
        sortButton.tap()

        XCTAssertTrue(
            elementExistsWithin(app.buttons["Species"], timeout: 5),
            "AC 1: 'Species' sort option must appear in the sort action sheet"
        )
        dismissActionSheetIfPresent()
    }

    // MARK: - AC 6: Clear selection option

    func testSortActionSheetContainsClearSelectionOption() {
        guard openVillagersTab() else { return }
        let sortButton = app.navigationBars.buttons["arrow.up.arrow.down.circle"]
        guard elementExistsWithin(sortButton, timeout: 8) else {
            XCTFail("Sort button not found"); return
        }

        sortButton.tap()
        let nameOption = app.buttons["Name"]
        guard elementExistsWithin(nameOption, timeout: 5) else {
            XCTFail("Name sort option not found"); return
        }
        nameOption.tap()

        let activeButton = app.navigationBars.buttons["arrow.up.arrow.down.circle.fill"]
        guard elementExistsWithin(activeButton, timeout: 5) else {
            XCTFail("Active sort button not found after selecting Name"); return
        }
        activeButton.tap()

        XCTAssertTrue(
            elementExistsWithin(app.buttons["Clear Selection"], timeout: 5),
            "AC 6: 'Clear Selection' option must appear in the sort action sheet when a sort is active"
        )
        dismissActionSheetIfPresent()
    }
}
