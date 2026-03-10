import XCTest
import SwiftUI
@testable import AC_Helper

final class AnvilTask8F2PTests: XCTestCase {

    // MARK: - TurnipsAveragePriceRow accepts both prices and minMax

    func testAveragePriceRowInitWithPricesAndMinMax() {
        let row = TurnipsAveragePriceRow(
            label: "Mon",
            prices: [90, 95],
            minMaxPrices: [[85, 95], [90, 100]]
        )
        XCTAssertEqual(row.label, "Mon")
        XCTAssertEqual(row.prices, [90, 95])
        XCTAssertEqual(row.minMaxPrices, [[85, 95], [90, 100]])
    }

    // MARK: - TurnipsMinMaxPriceRow existence

    func testMinMaxPriceRowConstruction() {
        let row = TurnipsMinMaxPriceRow(
            label: "Wed",
            prices: [[60, 90], [80, 120]],
            averagePrices: [75, 100]
        )
        XCTAssertEqual(row.label, "Wed")
        XCTAssertEqual(row.prices, [[60, 90], [80, 120]])
        XCTAssertEqual(row.averagePrices, [75, 100])
    }

    // MARK: - TurnipsPriceRow protocol conformance

    func testAveragePriceRowConformsToTurnipsPriceRow() {
        func require<T: TurnipsPriceRow>(_: T.Type) {}
        require(TurnipsAveragePriceRow.self)
    }

    func testMinMaxPriceRowConformsToTurnipsPriceRow() {
        func require<T: TurnipsPriceRow>(_: T.Type) {}
        require(TurnipsMinMaxPriceRow.self)
    }

    // MARK: - Entered value detection

    func testIsEnteredTrueWhenPriceEqualsMinMax() {
        let row = TurnipsAveragePriceRow(
            label: "Mon",
            prices: [90, 95],
            minMaxPrices: [[90, 90], [80, 110]]
        )
        XCTAssertTrue(row.isEntered(meridian: .am),
                       "AM should be entered when avg == min == max")
        XCTAssertFalse(row.isEntered(meridian: .pm),
                        "PM should not be entered when min != max")
    }

    func testIsEnteredFalseForPredictions() {
        let row = TurnipsAveragePriceRow(
            label: "Tue",
            prices: [85, 100],
            minMaxPrices: [[70, 100], [80, 120]]
        )
        XCTAssertFalse(row.isEntered(meridian: .am))
        XCTAssertFalse(row.isEntered(meridian: .pm))
    }

    func testIsEnteredOnMinMaxRow() {
        let row = TurnipsMinMaxPriceRow(
            label: "Fri",
            prices: [[110, 110], [60, 90]],
            averagePrices: [110, 75]
        )
        XCTAssertTrue(row.isEntered(meridian: .am))
        XCTAssertFalse(row.isEntered(meridian: .pm))
    }

    // MARK: - View type-erasure helpers

    func testEraseToAnyViewExists() {
        let _: AnyView = Text("test").eraseToAnyView()
    }
}
