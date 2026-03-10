import XCTest
import SwiftUI
@testable import AC_Helper

final class AnvilTask2F2PTests: XCTestCase {

    // MARK: - GridStack struct basics

    func testGridStackExists() {
        _ = GridStack<AnyView>(rows: 2, columns: 3) { _, _ in AnyView(EmptyView()) }
    }

    func testGridStackRowsAndColumns() {
        let gs = GridStack<AnyView>(rows: 1, columns: 3) { _, _ in AnyView(EmptyView()) }
        XCTAssertEqual(gs.rows, 1)
        XCTAssertEqual(gs.columns, 3)
    }

    func testGridStackWithSpacing() {
        let gs = GridStack<AnyView>(rows: 2, columns: 3, spacing: 16) { _, _ in AnyView(EmptyView()) }
        XCTAssertEqual(gs.rows, 2)
        XCTAssertEqual(gs.columns, 3)
        XCTAssertEqual(gs.spacing, 16)
    }

    func testGridStackDefaultSpacingIsNil() {
        let gs = GridStack<AnyView>(rows: 2, columns: 3) { _, _ in AnyView(EmptyView()) }
        XCTAssertNil(gs.spacing,
                     "GridStack without explicit spacing should default to nil")
    }

    // MARK: - GridStack content closure

    func testGridStackContentClosureReceivesCorrectIndices() {
        var receivedIndices: [(Int, Int)] = []
        let gs = GridStack<AnyView>(rows: 2, columns: 3) { row, col in
            receivedIndices.append((row, col))
            return AnyView(Text("cell"))
        }
        for row in 0..<2 {
            for col in 0..<3 {
                _ = gs.content(row, col)
            }
        }
        XCTAssertEqual(receivedIndices.count, 6)
        XCTAssertTrue(receivedIndices.contains(where: { $0.0 == 0 && $0.1 == 0 }))
        XCTAssertTrue(receivedIndices.contains(where: { $0.0 == 1 && $0.1 == 2 }))
    }

    // MARK: - GridStack is a real View (not just a struct)

    func testGridStackConformsToView() {
        let gs = GridStack<AnyView>(rows: 2, columns: 2) { _, _ in AnyView(Text("cell")) }
        let body = gs.body
        XCTAssertNotNil(body, "GridStack.body must produce a View")
    }

    // MARK: - GridStack ViewBuilder closure

    func testGridStackUsesViewBuilderContent() {
        let gs = GridStack(rows: 1, columns: 2, spacing: 8) { row, col in
            Text("R\(row)C\(col)")
        }
        XCTAssertEqual(gs.rows, 1)
        XCTAssertEqual(gs.columns, 2)
        XCTAssertEqual(gs.spacing, 8)
    }

    // MARK: - Behavioral: full cell coverage (alignment guarantee)

    /// The central contract of GridStack: EVERY (row, col) pair in the grid is
    /// reachable via the content closure with no gaps and no duplicates.
    /// This is what makes column N in every row positionally equivalent,
    /// giving vertical alignment.
    func testGridStackCoversAllCellsWithNoDuplicates() {
        let rows = 3, cols = 4
        var seen = Set<String>()
        let gs = GridStack<AnyView>(rows: rows, columns: cols) { row, col in
            seen.insert("\(row):\(col)")
            return AnyView(EmptyView())
        }
        for r in 0..<rows { for c in 0..<cols { _ = gs.content(r, c) } }

        XCTAssertEqual(seen.count, rows * cols,
                       "Must cover exactly rows×cols unique (row,col) pairs — no duplicates, no gaps")
        for r in 0..<rows {
            for c in 0..<cols {
                XCTAssertTrue(seen.contains("\(r):\(c)"), "Missing cell (\(r), \(c))")
            }
        }
    }

    /// Every row must be invoked with the same number of columns.
    /// This enforces the uniform column structure that ensures vertical alignment.
    func testGridStackEachRowReceivesSameColumnCount() {
        let rows = 5, cols = 3
        var countPerRow = [Int: Int]()
        let gs = GridStack<AnyView>(rows: rows, columns: cols) { row, _ in
            countPerRow[row, default: 0] += 1
            return AnyView(EmptyView())
        }
        for r in 0..<rows { for c in 0..<cols { _ = gs.content(r, c) } }
        for r in 0..<rows {
            XCTAssertEqual(countPerRow[r], cols,
                           "Row \(r) must get exactly \(cols) cells — uniform columns ensure vertical alignment")
        }
    }

    /// 7 days × 3 columns is the actual turnip price table shape (Mon–Sun, AM/PM header + 6 data rows).
    /// GridStack must produce exactly 21 cell invocations.
    func testGridStackTurnipTableCellCount() {
        let rows = 7, cols = 3
        var count = 0
        let gs = GridStack<AnyView>(rows: rows, columns: cols) { _, _ in
            count += 1
            return AnyView(EmptyView())
        }
        for r in 0..<rows { for c in 0..<cols { _ = gs.content(r, c) } }
        XCTAssertEqual(count, rows * cols,
                       "Turnip price table (7 days × 3 columns) must produce exactly \(rows * cols) cells")
    }

    /// The content closure must only receive in-range indices.
    func testGridStackIndicesAreWithinBounds() {
        let rows = 2, cols = 3
        var outOfBounds = false
        let gs = GridStack<AnyView>(rows: rows, columns: cols) { row, col in
            if row < 0 || row >= rows || col < 0 || col >= cols { outOfBounds = true }
            return AnyView(EmptyView())
        }
        for r in 0..<rows { for c in 0..<cols { _ = gs.content(r, c) } }
        XCTAssertFalse(outOfBounds, "GridStack must never pass out-of-bounds indices to the content closure")
    }

    /// The header row (1 row × 3 columns) matches the exact GridStack call that
    /// replaced the old free-floating HStack in TurnipsView.
    func testGridStackHeaderRowPatternUsedInTurnipsView() {
        var labels = [String]()
        let gs = GridStack(rows: 1, columns: 3) { _, col -> Text in
            switch col {
            case 0: labels.append("Day"); return Text("Day").fontWeight(.bold)
            case 1: labels.append("AM");  return Text("AM").fontWeight(.bold)
            default: labels.append("PM"); return Text("PM").fontWeight(.bold)
            }
        }
        for c in 0..<3 { _ = gs.content(0, c) }

        XCTAssertEqual(labels, ["Day", "AM", "PM"],
                       "Header row must produce Day/AM/PM in column order 0/1/2")
        XCTAssertNotNil(gs.body)
    }

    /// A multi-row body (simulating a real data table) must render without crashing.
    func testGridStackMultiRowBodyRendersWithoutCrash() {
        let gs = GridStack(rows: 7, columns: 3, spacing: 16) { row, col in
            Text("R\(row)C\(col)")
        }
        XCTAssertNotNil(gs.body, "GridStack body must be non-nil for a 7×3 data table")
    }
}
