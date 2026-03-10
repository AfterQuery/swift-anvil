import XCTest
import SwiftUI
@testable import AC_Helper
import Backend

final class AnvilTask5F2PTests: XCTestCase {

    // MARK: - Chore Model

    func testChoreDefaultInit() {
        let chore = Chore()
        XCTAssertTrue(chore.title.isEmpty)
        XCTAssertTrue(chore.description.isEmpty)
        XCTAssertFalse(chore.isFinished)
    }

    func testChoreCustomInit() {
        let chore = Chore(title: "Water flowers", description: "In garden", isFinished: true)
        XCTAssertEqual(chore.title, "Water flowers")
        XCTAssertEqual(chore.description, "In garden")
        XCTAssertTrue(chore.isFinished)
    }

    func testChoreEqualityIsById() {
        let a = Chore(title: "Task A")
        let b = Chore(title: "Task B")
        XCTAssertNotEqual(a, b, "Chores with different IDs should not be equal")
        XCTAssertEqual(a, a, "A Chore should be equal to itself")
    }

    // MARK: - UserCollection Chore Operations

    func testAddChoreAppendsToCollection() {
        let collection = UserCollection(iCloudDisabled: true)
        let initialCount = collection.chores.count
        let chore = Chore(title: "Water flowers")
        collection.addChore(chore)
        XCTAssertEqual(collection.chores.count, initialCount + 1)
        XCTAssertEqual(collection.chores.last?.title, "Water flowers")
    }

    func testToggleChoreTogglesFinishedState() {
        let collection = UserCollection(iCloudDisabled: true)
        let chore = Chore(title: "Sell turnips")
        collection.addChore(chore)
        let idx = collection.chores.count - 1
        XCTAssertFalse(collection.chores[idx].isFinished)

        collection.toggleChore(chore)
        XCTAssertTrue(collection.chores[idx].isFinished,
                      "First toggle should mark chore as finished")

        collection.toggleChore(chore)
        XCTAssertFalse(collection.chores[idx].isFinished,
                       "Second toggle should unmark chore")
    }

    func testResetChoresResetsAllToNotFinished() {
        let collection = UserCollection(iCloudDisabled: true)
        let a = Chore(title: "A")
        let b = Chore(title: "B")
        collection.addChore(a)
        collection.addChore(b)
        collection.toggleChore(a)
        collection.toggleChore(b)

        collection.resetChores()
        XCTAssertTrue(collection.chores.allSatisfy { !$0.isFinished },
                       "All chores should be reset to not finished")
    }

    func testDeleteChoreAtIndex() {
        let collection = UserCollection(iCloudDisabled: true)
        collection.addChore(Chore(title: "Keep"))
        collection.addChore(Chore(title: "Remove"))
        let countBefore = collection.chores.count
        collection.deleteChore(at: countBefore - 1)
        XCTAssertEqual(collection.chores.count, countBefore - 1,
                        "Deleting a chore should reduce the count by one")
    }

    // MARK: - TodaySection Integration

    func testTodaySectionChoresInDefaults() {
        let defaults = TodaySection.defaultSectionList
        XCTAssertTrue(defaults.contains { $0.name == .chores },
                       "Chores should appear in the default dashboard sections")
    }

    // MARK: - ChoreFormViewModel

    func testChoreFormViewModelInitWithNilCreatesEmptyChore() {
        let vm = ChoreFormViewModel(chore: nil)
        XCTAssertTrue(vm.chore.title.isEmpty)
        XCTAssertTrue(vm.chore.description.isEmpty)
        XCTAssertFalse(vm.chore.isFinished)
    }

    func testChoreFormViewModelInitWithChorePreservesValues() {
        let chore = Chore(title: "Gift villager", description: "Birthday present")
        let vm = ChoreFormViewModel(chore: chore)
        XCTAssertEqual(vm.chore.title, "Gift villager")
        XCTAssertEqual(vm.chore.description, "Birthday present")
    }

    // MARK: - ChoreListViewModel computed properties

    func testShouldShowResetButtonFalseWhenNoneFinished() {
        let vm = ChoreListViewModel()
        vm.chores = [Chore(title: "A", isFinished: false)]
        XCTAssertFalse(vm.shouldShowResetButton,
                        "Reset button should be hidden when no chores are finished")
    }

    func testShouldShowResetButtonTrueWhenSomeFinished() {
        let vm = ChoreListViewModel()
        vm.chores = [Chore(title: "A", isFinished: true),
                     Chore(title: "B", isFinished: false)]
        XCTAssertTrue(vm.shouldShowResetButton,
                       "Reset button should show when at least one chore is finished")
    }

    func testShouldShowDescriptionViewWhenEmpty() {
        let vm = ChoreListViewModel()
        vm.chores = []
        XCTAssertTrue(vm.shouldShowDescriptionView,
                       "Description view should show when there are no chores")
    }

    func testShouldShowDescriptionViewFalseWhenNotEmpty() {
        let vm = ChoreListViewModel()
        vm.chores = [Chore(title: "A")]
        XCTAssertFalse(vm.shouldShowDescriptionView,
                        "Description view should hide when chores exist")
    }

    // MARK: - TodayChoresSectionViewModel computed properties

    func testTotalChoresCount() {
        let vm = TodayChoresSectionViewModel()
        vm.chores = [Chore(title: "A"), Chore(title: "B"), Chore(title: "C")]
        XCTAssertEqual(vm.totalChoresCount, 3)
    }

    func testCompleteChoresCount() {
        let vm = TodayChoresSectionViewModel()
        vm.chores = [
            Chore(title: "A", isFinished: true),
            Chore(title: "B", isFinished: false),
            Chore(title: "C", isFinished: true),
        ]
        XCTAssertEqual(vm.completeChoresCount, 2)
    }

    func testCompleteChoresCountZeroWhenNoneFinished() {
        let vm = TodayChoresSectionViewModel()
        vm.chores = [Chore(title: "A"), Chore(title: "B")]
        XCTAssertEqual(vm.completeChoresCount, 0)
    }
}
