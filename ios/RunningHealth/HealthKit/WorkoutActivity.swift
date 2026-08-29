import Foundation
import HealthKit

/// Renders an activity type the way the Apple Health XML export does, so rows
/// produced by this app group with rows produced by an `export.zip` import.
enum WorkoutActivity {
    private static let names: [HKWorkoutActivityType: String] = [
        .running: "Running",
        .walking: "Walking",
        .cycling: "Cycling",
        .hiking: "Hiking",
        .swimming: "Swimming",
        .rowing: "Rowing",
        .elliptical: "Elliptical",
        .highIntensityIntervalTraining: "HighIntensityIntervalTraining",
        .traditionalStrengthTraining: "TraditionalStrengthTraining",
        .functionalStrengthTraining: "FunctionalStrengthTraining",
        .yoga: "Yoga",
    ]

    static func exportIdentifier(for type: HKWorkoutActivityType) -> String {
        "HKWorkoutActivityType" + (names[type] ?? "Other")
    }

    static func displayName(for type: HKWorkoutActivityType) -> String {
        names[type] ?? "Other"
    }
}
