import Foundation
import HealthKit

/// Persists `HKQueryAnchor`s so each sync only pulls what HealthKit added,
/// changed or deleted since the previous run.
struct AnchorStore {
    private let defaults: UserDefaults
    private let prefix = "healthkit.anchor."

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    func anchor(for key: String) -> HKQueryAnchor? {
        guard let data = defaults.data(forKey: prefix + key) else { return nil }
        return try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: data)
    }

    func setAnchor(_ anchor: HKQueryAnchor?, for key: String) {
        guard let anchor else {
            defaults.removeObject(forKey: prefix + key)
            return
        }
        let data = try? NSKeyedArchiver.archivedData(
            withRootObject: anchor,
            requiringSecureCoding: true
        )
        defaults.set(data, forKey: prefix + key)
    }

    func reset() {
        for key in defaults.dictionaryRepresentation().keys where key.hasPrefix(prefix) {
            defaults.removeObject(forKey: key)
        }
    }
}
