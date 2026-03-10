import XCTest
import SwiftUI
@testable import AC_Helper
import Backend

final class AnvilTask1F2PTests: XCTestCase {

    // MARK: - Helper: build an Item with specific active-month/time data

    private func makeItem(name: String = "TestFish",
                          activeTimes: [String] = ["0", "0"],
                          allMonths: Bool = true) -> Item {
        var monthEntries: [String] = []
        let months = allMonths ? 0..<12 : 0..<0
        for m in months {
            let times = activeTimes.map { "\"\($0)\"" }.joined(separator: ",")
            monthEntries.append("\"\(m)\":{\"activeTimes\":[\(times)]}")
        }
        let monthsJSON = monthEntries.joined(separator: ",")
        let json: String
        if allMonths {
            json = "{\"name\":\"\(name)\",\"category\":\"Fish\",\"filename\":\"test\",\"activeMonths\":{\(monthsJSON)}}"
        } else {
            json = "{\"name\":\"\(name)\",\"category\":\"Fish\",\"filename\":\"test\"}"
        }
        return try! JSONDecoder().decode(Item.self, from: json.data(using: .utf8)!)
    }

    // MARK: - Backend: isActiveAtThisHour()

    func testIsActiveAtThisHourReturnsFalseWithoutActiveMonths() {
        let item = makeItem(activeTimes: ["0", "0"], allMonths: false)
        XCTAssertFalse(item.isActiveAtThisHour(),
                       "Item without active hours data should not be active this hour")
    }

    func testIsActiveAtThisHourReturnsTrueWhenActiveAllYearAndAllDay() {
        let item = makeItem(activeTimes: ["0", "0"], allMonths: true)
        XCTAssertTrue(item.isActiveAtThisHour(),
                      "Item active all year and all day should always be active at this hour")
    }

    // MARK: - Backend: isActiveThisMonth() (renamed from isActive)

    func testIsActiveThisMonthMethodExists() {
        let item = makeItem(allMonths: true)
        XCTAssertTrue(item.isActiveThisMonth(),
                      "Item active all 12 months should be active this month")
    }

    func testIsActiveThisMonthReturnsFalseForNoMonths() {
        let item = makeItem(allMonths: false)
        XCTAssertFalse(item.isActiveThisMonth(),
                       "Item with no active months should not be active this month")
    }

    // MARK: - Backend: filterActiveThisMonth() collection method

    func testFilterActiveThisMonthOnCollection() {
        let allYear = makeItem(name: "AllYear", allMonths: true)
        let noMonth = makeItem(name: "NoMonth", allMonths: false)
        let items: [Item] = [allYear, noMonth]
        let filtered = items.filterActiveThisMonth()
        XCTAssertTrue(filtered.contains(where: { $0.name == "AllYear" }),
                      "All-year item should pass filterActiveThisMonth()")
        XCTAssertFalse(filtered.contains(where: { $0.name == "NoMonth" }),
                       "No-month item should be excluded by filterActiveThisMonth()")
    }

    // MARK: - CritterInfo: toCatchNow / toCatchLater

    func testCritterInfoHasToCatchNowAndToCatchLater() {
        let info = ActiveCrittersViewModel.CritterInfo(
            active: [], new: [], leaving: [], caught: [],
            toCatchNow: [], toCatchLater: []
        )
        XCTAssertTrue(info.toCatchNow.isEmpty)
        XCTAssertTrue(info.toCatchLater.isEmpty)
    }

    func testCritterInfoStoresNowAndLaterItems() {
        let nowItem = makeItem(name: "NowFish")
        let laterItem = makeItem(name: "LaterFish")

        let info = ActiveCrittersViewModel.CritterInfo(
            active: [], new: [], leaving: [], caught: [],
            toCatchNow: [nowItem], toCatchLater: [laterItem]
        )
        XCTAssertEqual(info.toCatchNow.count, 1)
        XCTAssertEqual(info.toCatchLater.count, 1)
    }

    func testCritterInfoDoesNotHaveOldToCatchProperty() {
        let info = ActiveCrittersViewModel.CritterInfo(
            active: [], new: [], leaving: [], caught: [],
            toCatchNow: [], toCatchLater: []
        )
        let mirror = Mirror(reflecting: info)
        let labels = mirror.children.compactMap { $0.label }
        XCTAssertFalse(labels.contains("toCatch"),
                       "Old combined 'toCatch' property should be replaced by toCatchNow/toCatchLater")
        XCTAssertTrue(labels.contains("toCatchNow"))
        XCTAssertTrue(labels.contains("toCatchLater"))
    }

    // MARK: - French localization

    func testFrenchLocalizationForToCatchNow() {
        guard let frPath = Bundle.main.path(forResource: "fr", ofType: "lproj"),
              let frBundle = Bundle(path: frPath) else {
            XCTFail("fr.lproj should exist")
            return
        }
        let translation = frBundle.localizedString(
            forKey: "To catch now",
            value: "MISSING",
            table: nil
        )
        XCTAssertNotEqual(translation, "MISSING",
                          "French should have a translation for 'To catch now'")
    }

    func testFrenchLocalizationForToCatchLater() {
        guard let frPath = Bundle.main.path(forResource: "fr", ofType: "lproj"),
              let frBundle = Bundle(path: frPath) else {
            XCTFail("fr.lproj should exist")
            return
        }
        let translation = frBundle.localizedString(
            forKey: "To catch later",
            value: "MISSING",
            table: nil
        )
        XCTAssertNotEqual(translation, "MISSING",
                          "French should have a translation for 'To catch later'")
    }
}
