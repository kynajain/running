import XCTest

@testable import RunningHealth

final class PipelineRecordsTests: XCTestCase {
    private let start = Date(timeIntervalSince1970: 1_770_000_000)

    private func json(_ record: PipelineRecord) throws -> [String: Any] {
        let data = try PipelineCoding.encoder.encode(record)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    func testSampleEncodesExactlyTheBackendKeys() throws {
        let sample = HealthSampleRecord(
            metric: .hrvSDNN,
            value: 48,
            start: start,
            end: start.addingTimeInterval(1),
            source: "Apple Watch"
        )
        let envelope = try json(.sample(sample))
        XCTAssertEqual(envelope["type"] as? String, "sample")
        let record = try XCTUnwrap(envelope["record"] as? [String: Any])
        XCTAssertEqual(
            Set(record.keys),
            ["metric", "value", "unit", "start", "end", "source"]
        )
        XCTAssertEqual(record["metric"] as? String, "hrv_sdnn")
        XCTAssertEqual(record["unit"] as? String, "ms")
        XCTAssertEqual(record["start"] as? String, "2026-02-02T02:40:00Z")
    }

    func testWorkoutEncodesSnakeCasedKeysAndNestedRoute() throws {
        let workout = WorkoutSessionRecord(
            id: "1D0B1A5E",
            activity: "HKWorkoutActivityTypeRunning",
            start: start,
            end: start.addingTimeInterval(600),
            distanceM: 1200,
            route: [GeoPointRecord(lat: 51.5387, lon: -0.0166, elevationM: 12, timestamp: start)],
            samples: []
        )
        let record = try XCTUnwrap(try json(.workout(workout))["record"] as? [String: Any])
        XCTAssertEqual(
            Set(record.keys),
            ["id", "activity", "start", "end", "distance_m", "route", "samples"]
        )
        let route = try XCTUnwrap(record["route"] as? [[String: Any]])
        XCTAssertEqual(Set(route[0].keys), ["lat", "lon", "elevation_m", "timestamp"])
    }

    func testNDJSONIsOneRecordPerLine() throws {
        let sample = HealthSampleRecord(
            metric: .heartRate,
            value: 130,
            start: start,
            end: start,
            source: "Watch"
        )
        let payload = try PipelineCoding.ndjson([.sample(sample), .sample(sample)])
        let lines = String(decoding: payload, as: UTF8.self)
            .split(separator: "\n", omittingEmptySubsequences: false)
        XCTAssertEqual(lines.count, 3)
        XCTAssertEqual(lines.last, "")
    }

    func testRoundTripPreservesRecords() throws {
        let sample = HealthSampleRecord(
            metric: .activeEnergy,
            value: 12.5,
            start: start,
            end: start.addingTimeInterval(60),
            source: "iPhone"
        )
        let data = try PipelineCoding.encoder.encode(PipelineRecord.sample(sample))
        let decoded = try PipelineCoding.decoder.decode(PipelineRecord.self, from: data)
        XCTAssertEqual(decoded, .sample(sample))
    }

    func testMetricUnitLabelsMatchTheAppleExport() {
        XCTAssertEqual(HealthMetric.heartRate.unitLabel, "count/min")
        XCTAssertEqual(HealthMetric.activeEnergy.unitLabel, "kcal")
        XCTAssertEqual(HealthMetric.allCases.map(\.rawValue).sorted(), [
            "active_energy", "heart_rate", "hrv_sdnn", "respiratory_rate", "resting_heart_rate",
        ])
    }
}
