import XCTest

@testable import RunningHealth

@MainActor
final class SyncSettingsTests: XCTestCase {
    private func settings() throws -> SyncSettings {
        let suite = try XCTUnwrap(UserDefaults(suiteName: "SyncSettingsTests-\(UUID())"))
        return SyncSettings(defaults: suite, keychain: KeychainStore(service: "test-\(UUID())"))
    }

    func testRejectsNonHTTPSAndMalformedEndpoints() throws {
        let settings = try settings()
        for endpoint in ["", "http://sync.example.com/ingest", "sync.example.com", "https://"] {
            settings.endpoint = endpoint
            XCTAssertNil(settings.endpointURL, "expected \(endpoint) to be rejected")
            XCTAssertFalse(settings.isUploadConfigured)
        }
    }

    func testAcceptsHTTPSEndpointWithSurroundingWhitespace() throws {
        let settings = try settings()
        settings.endpoint = "  https://sync.example.com/ingest  "
        XCTAssertEqual(settings.endpointURL?.absoluteString, "https://sync.example.com/ingest")
        XCTAssertTrue(settings.isUploadConfigured)
    }
}
