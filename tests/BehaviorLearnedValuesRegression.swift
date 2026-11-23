import Foundation

@main
struct BehaviorLearnedValuesRegression {
    static func main() throws {
        let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let rows = try JSONSerialization.jsonObject(with: data) as! [[String: Any]]
        for (index, row) in rows.enumerated() {
            let pattern = LearnedPattern(row["pattern"] as! [String: Any])!
            let prediction = LearnedPrediction(row["prediction"] as! [String: Any], index: index)!
            precondition(!pattern.statement.contains("You talk"))
            precondition(prediction.headline == "Possible contact window with Synthetic Person")
            precondition(prediction.because == (row["prediction"] as! [String: Any])["because"] as! String)
        }
        precondition(LearnedWeeklyValue.unknownReason(["value_reason": "incomplete_calendar_coverage"]).contains("coverage"))
        precondition(LearnedWeeklyValue.unknownReason(["value_reason": "missing_event_end_times"]).contains("end times"))
        precondition(LearnedWeeklyValue.unknownReason([:]).contains("available evidence"))
        // A Python-composed evidence label replaces "N of the last M times".
        let base: [String: Any] = ["id": "p1", "kind": "writing_style", "statement": "Synthetic statement",
                                   "support": 3, "opportunities": 9]
        var labelled = base
        labelled["stats"] = ["evidence_label": "from 42 sent emails"]
        precondition(LearnedPattern(labelled)!.evidence == "from 42 sent emails")
        var blank = base
        blank["stats"] = ["evidence_label": "  "]
        precondition(LearnedPattern(blank)!.evidence == "3 of the last 9 times")
        var wrongType = base
        wrongType["stats"] = ["evidence_label": 7]
        precondition(LearnedPattern(wrongType)!.evidence == "3 of the last 9 times")
        precondition(LearnedPattern(base)!.evidence == "3 of the last 9 times")
        // Weekly usage: lines when available, the note otherwise, nil when absent.
        precondition(LearnedWeeklyValue.usageLines(nil) == nil)
        precondition(LearnedWeeklyValue.usageLines("usage") == nil)
        precondition(LearnedWeeklyValue.usageLines(["available": true, "lines": ["Asked 12 times", 4, " "],
                                                    "note": "unused"]) == ["Asked 12 times"])
        precondition(LearnedWeeklyValue.usageLines(["available": false, "lines": ["hidden"],
                                                    "note": "Not enough use yet."]) == ["Not enough use yet."])
        precondition(LearnedWeeklyValue.usageLines(["available": 1, "lines": ["hidden"], "note": "n"]) == ["n"])
        precondition(LearnedWeeklyValue.usageLines(["available": true, "lines": []]) == nil)
        print("\(rows.count) source-bound Learned value checks passed")
    }
}
