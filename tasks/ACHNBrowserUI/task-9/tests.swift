import XCTest
@testable import AC_Helper
import Backend

final class AnvilTask9F2PTests: XCTestCase {

    var collection: UserCollection!

    override func setUp() {
        super.setUp()
        collection = UserCollection(iCloudDisabled: true)
    }

    // MARK: - Helpers

    private func makeVillager(id: Int, name: String, species: String = "Cat") -> Villager {
        let json = """
        {"id":\(id),"name":{"name-en":"\(name)"},"personality":"Normal","gender":"Female","species":"\(species)"}
        """
        return try! JSONDecoder().decode(Villager.self, from: json.data(using: .utf8)!)
    }

    // MARK: - TodaySection.Name

    func testVillagerVisitsSectionNameExists() {
        // .villagerVisits is a new case added by the patch.
        // This will fail to compile on the base commit.
        let _: TodaySection.Name = .villagerVisits
    }

    func testVillagerVisitsSectionInDefaultSectionList() {
        // The patch appends a villagerVisits section to defaultSectionList.
        // This will fail to compile on the base commit (case does not exist).
        let defaults = TodaySection.defaultSectionList
        XCTAssertTrue(
            defaults.contains(where: { $0.name == .villagerVisits }),
            "villagerVisits must appear in TodaySection.defaultSectionList"
        )
    }

    // MARK: - UserCollection.visitedResidents
    // NOTE: The actual solution uses visitedResidents / toggleVisitedResident /
    // resetVisitedResidents — NOT villagerVisits / toggleVillagerVisit /
    // resetVillagerVisits as the problem description suggests.

    func testVisitedResidentsStartsEmpty() {
        // visitedResidents is a new @Published property added by the patch.
        // This will fail to compile on the base commit.
        XCTAssertTrue(collection.visitedResidents.isEmpty,
                      "visitedResidents should be empty on a fresh UserCollection")
    }

    func testToggleVisitedResidentAddsVillager() {
        let villager = makeVillager(id: 1, name: "Apollo", species: "Eagle")
        collection.toggleVisitedResident(villager: villager)
        XCTAssertTrue(
            collection.visitedResidents.contains(where: { $0.id == villager.id }),
            "toggleVisitedResident should add the villager to visitedResidents"
        )
    }

    func testToggleVisitedResidentRemovesVillagerOnSecondCall() {
        let villager = makeVillager(id: 2, name: "Marina", species: "Octopus")
        collection.toggleVisitedResident(villager: villager)
        collection.toggleVisitedResident(villager: villager)
        XCTAssertFalse(
            collection.visitedResidents.contains(where: { $0.id == villager.id }),
            "A second toggle should remove the villager from visitedResidents"
        )
    }

    func testToggleVisitedResidentCountIsOneAfterSingleAdd() {
        collection.toggleVisitedResident(villager: makeVillager(id: 3, name: "Zucker"))
        XCTAssertEqual(collection.visitedResidents.count, 1,
                       "visitedResidents should contain exactly one entry after one toggle")
    }

    func testResetVisitedResidentsClearsAll() {
        collection.toggleVisitedResident(villager: makeVillager(id: 4, name: "Bob"))
        collection.toggleVisitedResident(villager: makeVillager(id: 5, name: "Fang"))
        collection.resetVisitedResidents()
        XCTAssertTrue(collection.visitedResidents.isEmpty,
                      "resetVisitedResidents should clear all visited residents")
    }

    // MARK: - Reset-button visibility (via collection state)
    // TodayVillagerVisitsSection derives reset-button visibility directly from
    // collection.visitedResidents — there is no separate ViewModel for it.

    func testNoVisitedResidentsImpliesResetShouldBeHidden() {
        XCTAssertTrue(collection.visitedResidents.isEmpty,
                      "With no visits recorded the reset control should be hidden")
    }

    func testAtLeastOneVisitImpliesResetShouldBeVisible() {
        collection.toggleVisitedResident(villager: makeVillager(id: 6, name: "Tom Nook"))
        XCTAssertFalse(collection.visitedResidents.isEmpty,
                       "With at least one visit the reset control should become visible")
    }

    func testResetMakesButtonHiddenAgain() {
        collection.toggleVisitedResident(villager: makeVillager(id: 7, name: "Isabelle"))
        collection.resetVisitedResidents()
        XCTAssertTrue(collection.visitedResidents.isEmpty,
                      "After reset, visitedResidents should be empty and reset button hidden")
    }
}
