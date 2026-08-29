import Foundation

enum UploadError: LocalizedError {
    case notConfigured
    case server(status: Int)

    var errorDescription: String? {
        switch self {
        case .notConfigured:
            "Set an https sync endpoint in Settings first."
        case .server(let status):
            "The sync endpoint responded with HTTP \(status)."
        }
    }
}

/// POSTs NDJSON batches to the configured endpoint, retrying throttled and
/// transient failures the same way the Notion sink does on the backend.
struct HealthUploader {
    var session: URLSession = .shared
    var maxAttempts = 3

    func upload(_ records: [PipelineRecord], to endpoint: URL, token: String?) async throws {
        guard !records.isEmpty else { return }
        let body = try PipelineCoding.ndjson(records)
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.setValue("application/x-ndjson", forHTTPHeaderField: "Content-Type")
        if let token, !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }

        for attempt in 1...maxAttempts {
            let (_, response) = try await session.upload(for: request, from: body)
            guard let http = response as? HTTPURLResponse else { return }
            if (200..<300).contains(http.statusCode) { return }
            let retriable = http.statusCode == 429 || http.statusCode >= 500
            guard retriable, attempt < maxAttempts else {
                throw UploadError.server(status: http.statusCode)
            }
            let header = http.value(forHTTPHeaderField: "Retry-After").flatMap(Double.init)
            try await Task.sleep(for: .seconds(header ?? pow(2, Double(attempt))))
        }
    }
}

/// Writes the same NDJSON to a file so it can be handed to
/// `running sync --source ndjson --export <file>` without a server.
enum NDJSONExport {
    static func write(_ records: [PipelineRecord]) throws -> URL {
        let name = "running-\(Int(Date().timeIntervalSince1970)).ndjson"
        let url = URL.temporaryDirectory.appending(path: name)
        try PipelineCoding.ndjson(records).write(to: url, options: .atomic)
        return url
    }
}
