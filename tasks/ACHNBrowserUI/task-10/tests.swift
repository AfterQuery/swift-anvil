import XCTest
import SwiftUI
@testable import AC_Helper
import Backend

final class AnvilTask10F2PTests: XCTestCase {

    // MARK: - Design model

    func testDesignDefaultInit() {
        let design = Design()
        XCTAssertEqual(design.title, "")
        XCTAssertEqual(design.code, "")
        XCTAssertEqual(design.description, "")
    }

    func testDesignCustomInit() {
        let design = Design(title: "Jedi", code: "MOPJ15LTDSXC4T", description: "Jedi Tunic")
        XCTAssertEqual(design.title, "Jedi")
        XCTAssertEqual(design.code, "MOPJ15LTDSXC4T")
    }

    func testDesignIsIdentifiable() {
        func require<T: Identifiable>(_: T.Type) {}
        require(Design.self)
    }

    func testDesignIsEquatable() {
        let design = Design(title: "Test")
        XCTAssertEqual(design, design)
    }

    func testDesignValidCodeMA() {
        let design = Design(code: "MA-1234-5678-9012")
        XCTAssertTrue(design.hasValidCode)
    }

    func testDesignValidCodeMO() {
        let design = Design(code: "MO-1234-5678-9012")
        XCTAssertTrue(design.hasValidCode)
    }

    func testDesignInvalidCode() {
        let design = Design(code: "XX-1234-5678")
        XCTAssertFalse(design.hasValidCode)
    }

    // MARK: - UserCollection designs

    func testUserCollectionHasDesignsProperty() {
        let _: [Design] = UserCollection.shared.designs
    }

    // MARK: - DesignFormViewModel

    func testDesignFormViewModelInitWithNil() {
        let vm = DesignFormViewModel(design: nil)
        XCTAssertEqual(vm.design.title, "")
    }

    func testDesignFormViewModelInitWithDesign() {
        let design = Design(title: "Test", code: "MA1234567890AB")
        let vm = DesignFormViewModel(design: design)
        XCTAssertEqual(vm.design.title, "Test")
    }

    // MARK: - DesignRowViewModel

    func testDesignRowViewModelCreatorCategory() {
        let vm = DesignRowViewModel(design: Design(title: "Sam", code: "MA667931515180"))
        XCTAssertEqual(vm.category, "Creator")
    }

    func testDesignRowViewModelItemCategory() {
        let vm = DesignRowViewModel(design: Design(title: "Tunic", code: "MOPJ15LTDSXC4T"))
        XCTAssertEqual(vm.category, "Item")
    }

    func testDesignRowViewModelFormattedCode() {
        let vm = DesignRowViewModel(design: Design(code: "MA667931515180"))
        XCTAssertEqual(vm.code, "MA-6679-3151-5180")
    }

    // MARK: - CollectionMoreDetailViewModel

    func testCollectionMoreDetailViewModelHasRows() {
        let vm = CollectionMoreDetailViewModel()
        XCTAssertEqual(vm.rows.count, 2)
    }

    func testCollectionMoreDetailViewModelRowCases() {
        let _: CollectionMoreDetailViewModel.Row = .critters
        let _: CollectionMoreDetailViewModel.Row = .designs
    }

    // MARK: - Tabs enum

    func testTabsHasMoreCase() {
        let _: Tabs = .more
    }

    // MARK: - MessageView

    func testMessageViewInits() {
        let _ = MessageView(string: "Test")
        let _ = MessageView(collectionName: "critters")
        let _ = MessageView(noResultsFor: "query")
    }
}
