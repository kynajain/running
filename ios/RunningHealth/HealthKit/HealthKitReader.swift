import CoreLocation
import Foundation
import HealthKit

enum HealthKitError: LocalizedError {
    case unavailable
    case authorizationDenied

    var errorDescription: String? {
        switch self {
        case .unavailable:
            "HealthKit is not available on this device."
        case .authorizationDenied:
            "Health access was denied. Enable it in Settings › Privacy › Health."
        }
    }
}

/// Reads quantity samples and workouts out of HealthKit, incrementally.
///
/// HealthKit never reports reads to the user and silently returns an empty set
/// for types the user declined, so a run that yields nothing is not an error.
struct HealthKitReader {
    let store: HKHealthStore
    var anchors = AnchorStore()

    static var readTypes: Set<HKObjectType> {
        var types: Set<HKObjectType> = [HKObjectType.workoutType(), HKSeriesType.workoutRoute()]
        for metric in HealthMetric.allCases {
            types.insert(metric.quantityType)
        }
        return types
    }

    static var isAvailable: Bool { HKHealthStore.isHealthDataAvailable() }

    func requestAuthorization() async throws {
        guard Self.isAvailable else { throw HealthKitError.unavailable }
        try await store.requestAuthorization(toShare: [], read: Self.readTypes)
    }

    /// `true` once the user has been shown the sheet, whatever they chose.
    /// HealthKit deliberately hides read-authorization state.
    func hasRequestedAuthorization() async -> Bool {
        guard Self.isAvailable else { return false }
        let status = try? await store.statusForAuthorizationRequest(
            toShare: [],
            read: Self.readTypes
        )
        return status == .unnecessary
    }

    // MARK: - Samples

    func samples(for metric: HealthMetric, since start: Date?) async throws -> [HealthSampleRecord] {
        let predicate: HKSamplePredicate<HKQuantitySample> = .quantitySample(
            type: metric.quantityType,
            predicate: start.map { HKQuery.predicateForSamples(withStart: $0, end: nil) }
        )
        let descriptor = HKAnchoredObjectQueryDescriptor(
            predicates: [predicate],
            anchor: anchors.anchor(for: metric.rawValue)
        )
        let result = try await descriptor.result(for: store)
        anchors.setAnchor(result.newAnchor, for: metric.rawValue)
        return result.addedSamples.map { sample in
            HealthSampleRecord(
                metric: metric,
                value: sample.quantity.doubleValue(for: metric.unit),
                start: sample.startDate,
                end: sample.endDate,
                source: sample.sourceRevision.source.name
            )
        }
    }

    // MARK: - Workouts

    func workouts(since start: Date?) async throws -> [WorkoutSessionRecord] {
        let predicate: HKSamplePredicate<HKWorkout> = .workout(
            start.map { HKQuery.predicateForSamples(withStart: $0, end: nil) }
        )
        let descriptor = HKAnchoredObjectQueryDescriptor(
            predicates: [predicate],
            anchor: anchors.anchor(for: "workout")
        )
        let result = try await descriptor.result(for: store)
        anchors.setAnchor(result.newAnchor, for: "workout")

        var records: [WorkoutSessionRecord] = []
        for workout in result.addedSamples {
            records.append(
                WorkoutSessionRecord(
                    id: workout.uuid.uuidString,
                    activity: WorkoutActivity.exportIdentifier(for: workout.workoutActivityType),
                    start: workout.startDate,
                    end: workout.endDate,
                    distanceM: distanceMetres(of: workout),
                    route: try await route(of: workout),
                    samples: []
                )
            )
        }
        return records
    }

    private func distanceMetres(of workout: HKWorkout) -> Double {
        let types: [HKQuantityTypeIdentifier] = [
            .distanceWalkingRunning, .distanceCycling, .distanceSwimming,
        ]
        for identifier in types {
            let quantity = workout.statistics(for: HKQuantityType(identifier))?.sumQuantity()
            if let metres = quantity?.doubleValue(for: .meter()), metres > 0 {
                return metres
            }
        }
        return 0
    }

    private func route(of workout: HKWorkout) async throws -> [GeoPointRecord] {
        let descriptor = HKAnchoredObjectQueryDescriptor(
            predicates: [.workoutRoute(HKQuery.predicateForObjects(from: workout))],
            anchor: nil
        )
        let routes = try await descriptor.result(for: store).addedSamples
        var points: [GeoPointRecord] = []
        for route in routes {
            points.append(contentsOf: try await locations(in: route).map(GeoPointRecord.init))
        }
        return points.sorted { $0.timestamp < $1.timestamp }
    }

    /// `HKWorkoutRouteQuery` streams locations in batches and keeps calling its
    /// handler until `done`, so the batches are accumulated before resuming.
    private func locations(in route: HKWorkoutRoute) async throws -> [CLLocation] {
        try await withCheckedThrowingContinuation { continuation in
            var collected: [CLLocation] = []
            let query = HKWorkoutRouteQuery(route: route) { query, batch, done, error in
                if let error {
                    self.store.stop(query)
                    continuation.resume(throwing: error)
                    return
                }
                collected.append(contentsOf: batch ?? [])
                if done {
                    continuation.resume(returning: collected)
                }
            }
            store.execute(query)
        }
    }
}

private extension GeoPointRecord {
    init(_ location: CLLocation) {
        self.init(
            lat: location.coordinate.latitude,
            lon: location.coordinate.longitude,
            elevationM: location.verticalAccuracy >= 0 ? location.altitude : nil,
            timestamp: location.timestamp
        )
    }
}
