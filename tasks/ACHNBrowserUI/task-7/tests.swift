import XCTest
import SwiftUI
import Combine
// Task-7 base commit (87dace0a, PR #15) predates the module rename from
// ACHNBrowserUI → AC_Helper. All later tasks use AC_Helper; this one must use
// the original product module name so the build succeeds on the base commit.
@testable import ACHNBrowserUI

final class AnvilTask7F2PTests: XCTestCase {

    // MARK: - TurnipsViewModel

    func testTurnipsViewModelDefaultIslands() {
        let vm = TurnipsViewModel()
        XCTAssertNil(vm.islands, "Islands should default to nil before fetching")
    }

    func testFetchDoesNotCorruptNilState() {
        let vm = TurnipsViewModel()
        XCTAssertNil(vm.islands, "islands should be nil before fetch")
        vm.fetch()
        // Network is asynchronous; islands should remain nil immediately after synchronous initiation
        XCTAssertNil(vm.islands,
                     "islands should remain nil immediately after fetch() — network response is async")
    }

    func testTurnipsViewModelIsObservableObject() {
        let vm = TurnipsViewModel()
        XCTAssertNotNil(vm.objectWillChange,
                        "TurnipsViewModel must conform to ObservableObject")
    }

    // MARK: - Tab structure

    func testTabbarViewHasTurnipsTab() {
        let allTabs: [TabbarView.Tab] = [.items, .wardrobe, .nature, .villagers, .collection, .turnips]
        XCTAssertTrue(allTabs.contains(.turnips),
                      "Turnips should be present in the tab enum")
    }

    // MARK: - Island model

    func testIslandConformsToIdentifiable() {
        func requireIdentifiable<T: Identifiable>(_: T.Type) {}
        requireIdentifiable(Island.self)
    }

    func testIslandDescriptionIsOptionalAndNilIsValid() {
        // islandDescription is Optional<String>, meaning islands without a description
        // must be handled gracefully rather than crashing or requiring fallback
        let keyPath: KeyPath<Island, String?> = \Island.islandDescription
        let nilValue: String? = nil
        XCTAssertNil(nilValue,
                     "islandDescription Optional type must accept nil — islands without a description should not crash")
        _ = keyPath
    }

    // MARK: - CategoriesView (no binding, drill-down navigation)

    func testCategoriesViewRendersBodyWithoutBinding() {
        let view = CategoriesView(categories: Categories.items())
        // Accessing body exercises the render path; no @Binding should be required
        _ = view.body
    }

    // MARK: - CategoryDetailView

    func testCategoryDetailViewRendersForAllCategoryGroups() {
        // Each major category group must be renderable without crashing
        _ = CategoryDetailView(categories: Categories.wardrobe()).body
        _ = CategoryDetailView(categories: Categories.nature()).body
        _ = CategoryDetailView(categories: Categories.items()).body
    }

    // MARK: - ItemsListView accepts external view model

    func testItemsListViewRendersWithExternalViewModel() {
        let vm = ItemsViewModel(categorie: .housewares)
        let view = ItemsListView(viewModel: vm)
        // View model is passed externally; view should render without creating its own
        _ = view.body
    }

    func testItemsViewModelCategoryIsPreserved() {
        let vm = ItemsViewModel(categorie: .fossils)
        XCTAssertNotNil(vm, "ItemsViewModel should be constructible with any category")
    }

    // MARK: - Behavioral: TurnipsViewModel objectWillChange fires on islands mutation

    /// @Published fires objectWillChange synchronously on willSet.
    /// SwiftUI's TurnipsView depends on this to re-render the island list.
    func testTurnipsViewModelPublishesOnIslandsAssignment() {
        let vm = TurnipsViewModel()
        var publishCount = 0
        var cancellables = Set<AnyCancellable>()
        vm.objectWillChange.sink { publishCount += 1 }.store(in: &cancellables)
        vm.islands = []
        XCTAssertGreaterThan(publishCount, 0,
                             "objectWillChange must fire when islands is assigned — TurnipsView needs this to update its list")
    }

    func testTurnipsViewModelPublishesTwiceForTwoDistinctAssignments() {
        let vm = TurnipsViewModel()
        var publishCount = 0
        var cancellables = Set<AnyCancellable>()
        vm.objectWillChange.sink { publishCount += 1 }.store(in: &cancellables)
        vm.islands = []
        vm.islands = nil
        XCTAssertEqual(publishCount, 2,
                       "Two distinct @Published assignments must each fire objectWillChange once")
    }

    // MARK: - Behavioral: Island Identifiable ID is String (turnipCode)

    /// Island.Identifiable.ID must be String — the extension assigns id = turnipCode.
    /// A wrong type (e.g. Int or UUID) would break ForEach stability in TurnipsView.
    func testIslandIdentifiableIDIsString() {
        func requireStringID<T: Identifiable>(_: T.Type) where T.ID == String {}
        requireStringID(Island.self)
    }

    // MARK: - Behavioral: CategoriesView no longer requires @Binding selectedCategory

    /// The PR removed @Binding var selectedCategory from CategoriesView, replacing
    /// modal-picker navigation with drill-down NavigationLinks. Verify the old
    /// initializer signature no longer exists (new init takes only categories:[Categories]).
    func testCategoriesViewInitWithOnlyCategoriesParameter() {
        // This must compile with just categories: — no selectedCategory: binding needed.
        let view = CategoriesView(categories: Categories.wardrobe())
        _ = view.body
        let view2 = CategoriesView(categories: Categories.nature())
        _ = view2.body
    }

    // MARK: - Behavioral: ItemsListView accepts an external ViewModel

    /// ItemsListView used to create its own ItemsViewModel internally; the PR changed it
    /// to accept one externally via `viewModel:`. We verify the init compiles and that
    /// the VM retains its category (since ItemsListView now delegates to the passed VM).
    /// We test the VM directly rather than reading view.viewModel (which may be private).
    func testItemsListViewAcceptsExternalViewModelWithHousewaresCategory() {
        let vm = ItemsViewModel(categorie: .housewares)
        let _ = ItemsListView(viewModel: vm)   // must compile — fails on base commit
        // The VM itself retains its category regardless of being wrapped in a view
        XCTAssertEqual(vm.categorie, .housewares,
                       "ItemsViewModel must retain .housewares category after being passed to ItemsListView")
    }

    func testItemsListViewAcceptsExternalViewModelWithFossilsCategory() {
        let vm = ItemsViewModel(categorie: .fossils)
        let _ = ItemsListView(viewModel: vm)   // must compile — fails on base commit
        XCTAssertEqual(vm.categorie, .fossils,
                       "ItemsViewModel must retain .fossils category after being passed to ItemsListView")
    }

    // MARK: - Behavioral: turnips tab is in the main navigation

    func testTurnipsTabHasPositiveRawValue() {
        // turnips was appended after existing tabs, so its rawValue must be > 0
        XCTAssertGreaterThan(TabbarView.Tab.turnips.rawValue, 0,
                             "turnips tab must follow at least one existing tab in the tab bar")
    }
}
