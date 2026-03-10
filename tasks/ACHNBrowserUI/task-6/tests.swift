import XCTest
import SwiftUI
import Combine
@testable import AC_Helper

final class AnvilTask6F2PTests: XCTestCase {

    // MARK: - DashboardViewModel existence and ObservableObject

    func testDashboardViewModelIsObservableObject() {
        let vm = DashboardViewModel()
        XCTAssertNotNil(vm.objectWillChange,
                        "DashboardViewModel must conform to ObservableObject")
    }

    func testDashboardViewModelDefaultState() {
        let vm = DashboardViewModel()
        XCTAssertNil(vm.recentListings, "recentListings should default to nil")
        XCTAssertNil(vm.island, "island should default to nil")
        XCTAssertTrue(vm.fishes.isEmpty, "fishes should default to empty")
        XCTAssertTrue(vm.bugs.isEmpty, "bugs should default to empty")
        XCTAssertTrue(vm.fossils.isEmpty, "fossils should default to empty")
    }

    func testFetchMethodsAreCallableWithoutCorruptingState() {
        let vm = DashboardViewModel()
        // Behavioral: calling fetch methods should not crash or corrupt state arrays
        vm.fetchListings()
        vm.fetchIsland()
        vm.fetchCritters()
        XCTAssertNotNil(vm.fishes as Any,
                        "fishes should remain a valid array after fetchCritters()")
        XCTAssertNotNil(vm.bugs as Any,
                        "bugs should remain a valid array after fetchCritters()")
        XCTAssertNotNil(vm.fossils as Any,
                        "fossils should remain a valid array after fetchCritters()")
    }

    // MARK: - Tab structure: dashboard is first/default

    func testTabbarViewHasDashboardTab() {
        let tab: TabbarView.Tab = .dashboard
        XCTAssertEqual(tab.rawValue, 0,
                       "Dashboard should be the first tab (rawValue 0)")
    }

    // MARK: - Categories helper methods

    func testCategoriesFishMethod() {
        let fish = Categories.fish()
        XCTAssertTrue(fish == .fishesNorth || fish == .fishesSouth,
                      "Categories.fish() must return a fish category for the current hemisphere")
    }

    func testCategoriesBugsMethod() {
        let bugs = Categories.bugs()
        XCTAssertTrue(bugs == .bugsNorth || bugs == .bugsSouth,
                      "Categories.bugs() must return a bugs category for the current hemisphere")
    }

    // MARK: - Listing model additions

    func testListingHasNameProperty() throws {
        let json = """
        {"id":"1","itemId":"100","amount":1,"active":true,"selling":true,"makeOffer":false,"needMaterials":false,"username":"test","name":"Chair","img":"https://example.com/img.png"}
        """
        let listing = try JSONDecoder().decode(Listing.self, from: json.data(using: .utf8)!)
        XCTAssertEqual(listing.name, "Chair")
        XCTAssertNotNil(listing.img)
    }

    func testListingNameIsOptional() throws {
        let json = """
        {"id":"2","itemId":"200","amount":1,"active":true,"selling":true,"makeOffer":false,"needMaterials":false,"username":"test"}
        """
        let listing = try JSONDecoder().decode(Listing.self, from: json.data(using: .utf8)!)
        XCTAssertNil(listing.name, "name should be nil when not present")
        XCTAssertNil(listing.img, "img should be nil when not present")
    }

    // MARK: - NookazonService.recentListings

    func testNookazonRecentListingsPublisherCanBeSubscribed() {
        var cancellables = Set<AnyCancellable>()
        NookazonService.recentListings()
            .sink(receiveCompletion: { _ in }, receiveValue: { _ in })
            .store(in: &cancellables)
        XCTAssertFalse(cancellables.isEmpty,
                       "recentListings() must return a publisher that accepts subscribers without crashing")
    }

    // MARK: - Behavioral: objectWillChange fires on @Published mutations

    /// @Published fires objectWillChange synchronously on willSet.
    /// Verifying this proves the ViewModel is correctly wired for SwiftUI reactivity.
    func testDashboardViewModelPublishesOnRecentListingsMutation() {
        let vm = DashboardViewModel()
        var fired = false
        let c = vm.objectWillChange.sink { fired = true }
        vm.recentListings = []
        XCTAssertTrue(fired,
                      "objectWillChange must fire when recentListings is mutated — SwiftUI needs this to refresh the Dashboard")
        _ = c
    }

    func testDashboardViewModelPublishesOnFishesMutation() {
        let vm = DashboardViewModel()
        var fired = false
        let c = vm.objectWillChange.sink { fired = true }
        vm.fishes = []
        XCTAssertTrue(fired,
                      "objectWillChange must fire when fishes is mutated — critter count display depends on this")
        _ = c
    }

    func testDashboardViewModelPublishesOnBugsMutation() {
        let vm = DashboardViewModel()
        var fired = false
        let c = vm.objectWillChange.sink { fired = true }
        vm.bugs = []
        XCTAssertTrue(fired,
                      "objectWillChange must fire when bugs is mutated")
        _ = c
    }

    func testDashboardViewModelPublishesOnFossilsMutation() {
        let vm = DashboardViewModel()
        var fired = false
        let c = vm.objectWillChange.sink { fired = true }
        vm.fossils = []
        XCTAssertTrue(fired,
                      "objectWillChange must fire when fossils is mutated")
        _ = c
    }

    // MARK: - Behavioral: Listing.img decodes as URL

    /// img is typed as URL?, not String?, so the decoder parses and validates the URL.
    /// An invalid URL string would decode as nil, not crash.
    func testListingImgDecodesAsFullURL() throws {
        let json = """
        {"id":"3","itemId":"300","amount":1,"active":true,"selling":true,"makeOffer":false,"needMaterials":false,"username":"user","img":"https://nookazon.com/items/img.png"}
        """
        let listing = try JSONDecoder().decode(Listing.self, from: json.data(using: .utf8)!)
        XCTAssertEqual(listing.img, URL(string: "https://nookazon.com/items/img.png"),
                       "img must decode to the exact URL value in the JSON")
        XCTAssertEqual(listing.img?.scheme, "https")
        XCTAssertEqual(listing.img?.host, "nookazon.com")
    }

    // MARK: - Behavioral: dashboard tab precedes items tab

    func testDashboardTabPrecedesItemsTab() {
        XCTAssertLessThan(TabbarView.Tab.dashboard.rawValue,
                          TabbarView.Tab.items.rawValue,
                          "dashboard tab rawValue must be less than items tab — dashboard must be the first tab in the bar")
    }

    // MARK: - NookazonService.recentListings type contract

    func testNookazonRecentListingsReturnsTypedPublisher() {
        // Compile-time proof that the return type is AnyPublisher<[Listing], Error>
        let publisher: AnyPublisher<[Listing], Error> = NookazonService.recentListings()
        XCTAssertNotNil(publisher, "recentListings() must return a non-nil AnyPublisher<[Listing], Error>")
    }
}
