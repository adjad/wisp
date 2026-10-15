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
        print("\(rows.count) source-bound Learned value checks passed")
    }
}
