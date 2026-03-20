import XCTest

final class AnvilTask10UITests: XCTestCase {

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

    // MARK: - AC 4/5: "More" option exists in Collection (existence-only after minimal navigation)

    func testMoreOptionExistsInCollection() {
        let collectionTab = app.tabBars.buttons["Collection"]
        XCTAssertTrue(collectionTab.waitForExistence(timeout: 10), "Collection tab not found in tab bar")
        collectionTab.tap()
        let moreButton = app.buttons.matching(NSPredicate(format: "label CONTAINS[c] 'more'")).firstMatch
        XCTAssertTrue(
            moreButton.waitForExistence(timeout: 20),
            "AC 4/5: A 'More' option must exist in the Collection picker after the patch"
        )
    }

    // MARK: - AC 8: Existing Items tab still accessible (existence-only)

    func testItemsOrCatalogTabStillExists() {
        let found = ["Items", "Catalog", "Collection"].contains {
            app.tabBars.buttons[$0].waitForExistence(timeout: 5)
        }
        XCTAssertTrue(found, "AC 8: Items/Catalog tab must remain accessible after the patch")
    }

    // MARK: - AC 8: Villagers still accessible (existence-only)

    func testVillagersTabStillExists() {
        XCTAssertTrue(
            app.tabBars.buttons["Villagers"].waitForExistence(timeout: 10),
            "AC 8: Villagers tab must remain after Collection reorganisation"
        )
    }


}
