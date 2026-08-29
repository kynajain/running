import Foundation

/// Where synced records go. The token lives in the keychain; the rest is
/// non-sensitive and stays in `UserDefaults`.
@MainActor
final class SyncSettings: ObservableObject {
    @Published var endpoint: String {
        didSet { defaults.set(endpoint, forKey: Keys.endpoint) }
    }

    @Published var backgroundDeliveryEnabled: Bool {
        didSet { defaults.set(backgroundDeliveryEnabled, forKey: Keys.backgroundDelivery) }
    }

    @Published var token: String {
        didSet { keychain.set(token.isEmpty ? nil : token, for: Keys.token) }
    }

    private enum Keys {
        static let endpoint = "sync.endpoint"
        static let backgroundDelivery = "sync.backgroundDelivery"
        static let token = "sync.token"
    }

    private let defaults: UserDefaults
    private let keychain: KeychainStore

    init(defaults: UserDefaults = .standard, keychain: KeychainStore = KeychainStore()) {
        self.defaults = defaults
        self.keychain = keychain
        self.endpoint = defaults.string(forKey: Keys.endpoint) ?? ""
        self.backgroundDeliveryEnabled = defaults.bool(forKey: Keys.backgroundDelivery)
        self.token = keychain.value(for: Keys.token) ?? ""
    }

    /// Only `https` is accepted: health records must not leave the device in clear text.
    var endpointURL: URL? {
        guard let url = URL(string: endpoint.trimmingCharacters(in: .whitespaces)),
              url.scheme?.lowercased() == "https",
              url.host != nil
        else { return nil }
        return url
    }

    var isUploadConfigured: Bool { endpointURL != nil }
}
