import Foundation
import WispCore

// Read-only input; JSONL observations go to stdout for the independent shared grader.
do {
    let paths = Array(CommandLine.arguments.dropFirst())
    guard !paths.isEmpty else {
        FileHandle.standardError.write(Data("Usage: workflow20-ios cases.json [more-cases.json ...]\nNo models, native sources or actions are executed.\n".utf8))
        exit(2)
    }
    var seen = Set<String>()
    let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
    var inputs: [Workflow20Input] = []
    for path in paths {
        let corpus = try Workflow20Corpus.load(Data(contentsOf: URL(fileURLWithPath: path), options: .mappedIfSafe))
        for input in corpus.inputs() {
            guard seen.insert(input.caseID).inserted else { throw ContractError.duplicateID }
            inputs.append(input)
        }
    }
    for input in inputs {
        var data = try encoder.encode(Workflow20FoundationAdapter().observe(input)); data.append(0x0a)
        FileHandle.standardOutput.write(data)
    }
} catch {
    FileHandle.standardError.write(Data("Contract consumption failed: \(error)\n".utf8)); exit(1)
}
