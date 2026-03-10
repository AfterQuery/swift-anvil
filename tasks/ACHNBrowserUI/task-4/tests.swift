import XCTest
import SwiftUI
@testable import AC_Helper
import Backend

final class AnvilTask4F2PTests: XCTestCase {

    // MARK: - Helpers

    private func makeVillager(id: Int, name: String, species: String) -> Villager {
        let json = """
        {"id":\(id),"name":{"name-en":"\(name)"},"personality":"Normal","gender":"Female","species":"\(species)"}
        """
        return try! JSONDecoder().decode(Villager.self, from: json.data(using: .utf8)!)
    }

    // MARK: - Structural

    func testSortEnumExists() {
        let _: VillagersViewModel.Sort = .name
        let _: VillagersViewModel.Sort = .species
    }

    func testSortDefaultsToNil() {
        let vm = VillagersViewModel()
        XCTAssertNil(vm.sort, "Sort should default to nil")
        XCTAssertTrue(vm.sortedVillagers.isEmpty)
    }

    func testSortedVillagersEmptyWithNoData() {
        let vm = VillagersViewModel()
        vm.villagers = []
        vm.sort = .name
        XCTAssertTrue(vm.sortedVillagers.isEmpty,
                       "sortedVillagers should be empty when no villagers are loaded")
    }

    func testClearingSortEmptiesSortedVillagers() {
        let vm = VillagersViewModel()
        vm.sort = .name
        vm.sort = nil
        XCTAssertTrue(vm.sortedVillagers.isEmpty,
                       "Clearing sort should empty the sorted villagers array")
    }

    // MARK: - Behavioral

    func testSortByNameProducesAlphabeticalOrder() {
        let vm = VillagersViewModel()
        vm.villagers = [
            makeVillager(id: 1, name: "Zucker", species: "Octopus"),
            makeVillager(id: 2, name: "Apollo", species: "Eagle"),
            makeVillager(id: 3, name: "Marina", species: "Octopus"),
        ]
        vm.sort = .name
        XCTAssertEqual(vm.sortedVillagers.count, 3)
        XCTAssertEqual(vm.sortedVillagers.first?.localizedName, "Apollo")
        XCTAssertEqual(vm.sortedVillagers.last?.localizedName, "Zucker")
    }

    func testSortBySpeciesProducesAlphabeticalOrder() {
        let vm = VillagersViewModel()
        vm.villagers = [
            makeVillager(id: 1, name: "Zucker", species: "Octopus"),
            makeVillager(id: 2, name: "Apollo", species: "Eagle"),
        ]
        vm.sort = .species
        XCTAssertEqual(vm.sortedVillagers.count, 2)
        XCTAssertEqual(vm.sortedVillagers.first?.species, "Eagle")
        XCTAssertEqual(vm.sortedVillagers.last?.species, "Octopus")
    }

    func testSortReversalOnRepeat() {
        let vm = VillagersViewModel()
        vm.villagers = [
            makeVillager(id: 1, name: "Zucker", species: "Octopus"),
            makeVillager(id: 2, name: "Apollo", species: "Eagle"),
        ]
        vm.sort = .name
        XCTAssertEqual(vm.sortedVillagers.first?.localizedName, "Apollo",
                       "First sort should be ascending")

        vm.sort = .name
        XCTAssertEqual(vm.sortedVillagers.first?.localizedName, "Zucker",
                       "Repeating the same sort should reverse to descending")
    }

    // MARK: - AC 7: Search results take priority over sorting

    func testSearchResultsTakePriorityOverSorting() {
        let testVillagers = [
            makeVillager(id: 1, name: "Zucker", species: "Octopus"),
            makeVillager(id: 2, name: "Apollo", species: "Eagle"),
            makeVillager(id: 3, name: "Marina", species: "Octopus"),
        ]
        // Pre-populate the static villager cache so the test VM skips the API fetch.
        // VillagersViewModel.init() calls fetch() only when cachedVillagers is empty;
        // setting .villagers on a seed VM populates the cache via didSet.
        let seed = VillagersViewModel()
        seed.villagers = testVillagers

        // This VM finds a non-empty cache and does NOT call fetch(), so no async
        // API overwrite can race with our manually-set villagers.
        let vm = VillagersViewModel()
        vm.sort = .name
        vm.searchText = "Apollo"
        // Drain the debounced DispatchQueue.main pipeline
        let exp = expectation(description: "searchResults populated after debounce")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { exp.fulfill() }
        waitForExpectations(timeout: 2.0)
        XCTAssertTrue(vm.searchResults.contains { $0.localizedName == "Apollo" },
                      "AC 7: When searching, searchResults must contain matches; search takes priority over sorting")
    }

    // MARK: - AC 8: French localization for sort strings

    func testFrenchLocalizationForSortStrings() {
        guard let frPath = Bundle.main.path(forResource: "fr", ofType: "lproj"),
              let frBundle = Bundle(path: frPath) else {
            XCTFail("AC 8: French localization bundle (fr.lproj) not found")
            return
        }
        let translation = frBundle.localizedString(forKey: "Sort villagers", value: "MISSING", table: nil)
        XCTAssertEqual(translation, "Trier les villageois",
                       "AC 8: French localization for sort strings must exist")
    }
}
