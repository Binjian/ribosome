import XCTest
import SwiftTreeSitter
import TreeSitterSiasun

final class TreeSitterSiasunTests: XCTestCase {
    func testCanLoadGrammar() throws {
        let parser = Parser()
        let language = Language(language: tree_sitter_siasun())
        XCTAssertNoThrow(try parser.setLanguage(language),
                         "Error loading Siasun grammar")
    }
}
