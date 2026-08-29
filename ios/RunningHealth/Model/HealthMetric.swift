import Foundation
import HealthKit

/// Metrics mirrored from `running.models.Metric` in the Python sync package.
/// The raw values and unit labels are what the backend expects on the wire.
enum HealthMetric: String, CaseIterable, Sendable {
    case heartRate = "heart_rate"
    case hrvSDNN = "hrv_sdnn"
    case restingHeartRate = "resting_heart_rate"
    case respiratoryRate = "respiratory_rate"
    case activeEnergy = "active_energy"

    var identifier: HKQuantityTypeIdentifier {
        switch self {
        case .heartRate: .heartRate
        case .hrvSDNN: .heartRateVariabilitySDNN
        case .restingHeartRate: .restingHeartRate
        case .respiratoryRate: .respiratoryRate
        case .activeEnergy: .activeEnergyBurned
        }
    }

    var quantityType: HKQuantityType {
        HKQuantityType(identifier)
    }

    var unit: HKUnit {
        switch self {
        case .heartRate, .restingHeartRate, .respiratoryRate:
            HKUnit.count().unitDivided(by: .minute())
        case .hrvSDNN:
            .secondUnit(with: .milli)
        case .activeEnergy:
            .kilocalorie()
        }
    }

    /// Unit string the Apple Health XML export uses, so app-sourced rows are
    /// indistinguishable from export-sourced ones downstream.
    var unitLabel: String {
        switch self {
        case .heartRate, .restingHeartRate, .respiratoryRate: "count/min"
        case .hrvSDNN: "ms"
        case .activeEnergy: "kcal"
        }
    }

    var title: String {
        switch self {
        case .heartRate: "Heart rate"
        case .hrvSDNN: "HRV (SDNN)"
        case .restingHeartRate: "Resting heart rate"
        case .respiratoryRate: "Respiratory rate"
        case .activeEnergy: "Active energy"
        }
    }
}
