import XCTest
@testable import Backend

final class AnvilTask3F2PTests: XCTestCase {

    // MARK: - Helpers

    private func makeItem(
        name: String = "TestItem",
        filename: String = "anvil_test_item",
        variationCount: Int
    ) -> Item {
        let variants: String
        if variationCount > 0 {
            let entries = (0..<variationCount).map { i in
                "{\"id\":\(60000+i),\"content\":{\"image\":\"img\(i)\",\"colors\":[\"C\(i)\"]}}"
            }
            variants = entries.joined(separator: ",")
        } else {
            variants = ""
        }

        let json = """
        {
            "name":"\(name)",
            "category":"Housewares",
            "filename":"\(filename)",
            "variations":[\(variants)]
        }
        """
        return try! JSONDecoder().decode(Item.self, from: json.data(using: .utf8)!)
    }

    // MARK: - hasSomeVariations

    func testHasSomeVariationsWithMultiple() {
        let item = makeItem(variationCount: 3)
        XCTAssertTrue(item.hasSomeVariations,
                       "Item with 3 variants should report hasSomeVariations == true")
    }

    func testHasSomeVariationsWithOne() {
        let item = makeItem(variationCount: 1)
        XCTAssertFalse(item.hasSomeVariations,
                        "Item with 1 variant should not count as having variations")
    }

    func testHasSomeVariationsWithNone() {
        let item = makeItem(variationCount: 0)
        XCTAssertFalse(item.hasSomeVariations,
                        "Item with 0 variants should not count as having variations")
    }

    func testHasSomeVariationsWithExactlyTwo() {
        let item = makeItem(variationCount: 2)
        XCTAssertTrue(item.hasSomeVariations,
                       "Item with exactly 2 variants should report hasSomeVariations == true")
    }

    // MARK: - VariantsCompletionStatus enum

    func testVariantsCompletionStatusEnumCases() {
        let _: VariantsCompletionStatus = .unstarted
        let _: VariantsCompletionStatus = .partial
        let _: VariantsCompletionStatus = .complete
    }

    // MARK: - completionStatus(for:)

    func testCompletionStatusUnstartedWhenEmpty() {
        let item = makeItem(variationCount: 3)
        let dict: [String: [Variant]] = [:]
        XCTAssertEqual(dict.completionStatus(for: item), .unstarted)
    }

    func testCompletionStatusPartial() {
        let item = makeItem(variationCount: 3)
        let oneVariant = item.variations![0]
        let dict: [String: [Variant]] = ["anvil_test_item": [oneVariant]]
        XCTAssertEqual(dict.completionStatus(for: item), .partial)
    }

    func testCompletionStatusComplete() {
        let item = makeItem(variationCount: 3)
        let dict: [String: [Variant]] = ["anvil_test_item": item.variations!]
        XCTAssertEqual(dict.completionStatus(for: item), .complete)
    }

    func testCompletionStatusUnstartedForEmptyVariantsArray() {
        let item = makeItem(variationCount: 3)
        let dict: [String: [Variant]] = ["anvil_test_item": []]
        XCTAssertEqual(dict.completionStatus(for: item), .unstarted,
                        "Key present but empty array should be treated as unstarted")
    }

    // MARK: - toggleVariant auto-manages parent item (acceptance criterion 5)

    func testToggleVariantAutoAddsParentItem() {
        let collection = UserCollection(iCloudDisabled: true)
        let item = makeItem(filename: "anvil_auto_add_test", variationCount: 3)
        let variant = item.variations![0]

        XCTAssertFalse(collection.items.contains(item),
                        "Item should not be in collection before any toggle")

        _ = collection.toggleVariant(item: item, variant: variant)

        XCTAssertTrue(collection.items.contains(item),
                       "Toggling first variant should auto-add the parent item to the collection")
    }

    func testToggleLastVariantAutoRemovesParentItem() {
        let collection = UserCollection(iCloudDisabled: true)
        let item = makeItem(filename: "anvil_auto_remove_test", variationCount: 3)
        let variant = item.variations![0]

        _ = collection.toggleVariant(item: item, variant: variant)
        XCTAssertTrue(collection.items.contains(item))

        _ = collection.toggleVariant(item: item, variant: variant)
        XCTAssertFalse(collection.items.contains(item),
                        "Removing the last liked variant should auto-remove the parent item")
    }

    func testParentRemainsWhenSomeVariantsStillLiked() {
        let collection = UserCollection(iCloudDisabled: true)
        let item = makeItem(filename: "anvil_parent_remains_test", variationCount: 3)
        let v0 = item.variations![0]
        let v1 = item.variations![1]

        _ = collection.toggleVariant(item: item, variant: v0)
        _ = collection.toggleVariant(item: item, variant: v1)
        XCTAssertTrue(collection.items.contains(item))

        _ = collection.toggleVariant(item: item, variant: v0)
        XCTAssertTrue(collection.items.contains(item),
                       "Parent should remain in collection while at least one variant is still liked")
    }

    // MARK: - toggleVariant return value

    func testToggleVariantReturnValues() {
        let collection = UserCollection(iCloudDisabled: true)
        let item = makeItem(name: "ReturnTest", filename: "anvil_return_test", variationCount: 3)
        let variant = item.variations![0]

        let addResult = collection.toggleVariant(item: item, variant: variant)
        XCTAssertTrue(addResult, "toggleVariant should return true when adding a variant")

        let removeResult = collection.toggleVariant(item: item, variant: variant)
        XCTAssertFalse(removeResult, "toggleVariant should return false when removing a variant")
    }
}
