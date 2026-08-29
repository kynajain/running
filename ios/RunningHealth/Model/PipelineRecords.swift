import Foundation

/// Wire models for the `running` sync pipeline. The Python side declares
/// `extra="forbid"`, so these must encode exactly the documented keys.

struct HealthSampleRecord: Codable, Hashable, Sendable {
    var metric: HealthMetric.RawValue
    var value: Double
    var unit: String
    var start: Date
    var end: Date
    var source: String

    init(metric: HealthMetric, value: Double, start: Date, end: Date, source: String) {
        self.metric = metric.rawValue
        self.value = value
        self.unit = metric.unitLabel
        self.start = start
        self.end = end
        self.source = source
    }
}

struct GeoPointRecord: Codable, Hashable, Sendable {
    var lat: Double
    var lon: Double
    var elevationM: Double?
    var timestamp: Date

    enum CodingKeys: String, CodingKey {
        case lat
        case lon
        case elevationM = "elevation_m"
        case timestamp
    }
}

struct WorkoutSessionRecord: Codable, Hashable, Sendable {
    var id: String
    var activity: String
    var start: Date
    var end: Date
    var distanceM: Double
    var route: [GeoPointRecord]
    var samples: [HealthSampleRecord]

    enum CodingKeys: String, CodingKey {
        case id
        case activity
        case start
        case end
        case distanceM = "distance_m"
        case route
        case samples
    }
}

/// One NDJSON line. The envelope exists because the backend models forbid
/// extra keys, so the discriminator cannot live inside the record itself.
enum PipelineRecord: Codable, Hashable, Sendable {
    case sample(HealthSampleRecord)
    case workout(WorkoutSessionRecord)

    private enum CodingKeys: String, CodingKey {
        case type
        case record
    }

    private enum Kind: String, Codable {
        case sample
        case workout
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        switch try container.decode(Kind.self, forKey: .type) {
        case .sample:
            self = .sample(try container.decode(HealthSampleRecord.self, forKey: .record))
        case .workout:
            self = .workout(try container.decode(WorkoutSessionRecord.self, forKey: .record))
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .sample(let record):
            try container.encode(Kind.sample, forKey: .type)
            try container.encode(record, forKey: .record)
        case .workout(let record):
            try container.encode(Kind.workout, forKey: .type)
            try container.encode(record, forKey: .record)
        }
    }
}

enum PipelineCoding {
    /// `dateutil` requires an offset, and the backend normalises to UTC.
    static var encoder: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.withoutEscapingSlashes]
        return encoder
    }

    static var decoder: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }

    static func ndjson(_ records: [PipelineRecord]) throws -> Data {
        let encoder = self.encoder
        var payload = Data()
        for record in records {
            payload.append(try encoder.encode(record))
            payload.append(0x0A)
        }
        return payload
    }
}
