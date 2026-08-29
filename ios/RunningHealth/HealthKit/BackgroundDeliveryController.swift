import Foundation
import HealthKit

/// Wakes the app when HealthKit records new data. Observer queries must be
/// re-registered on every launch, and each update handler has to call its
/// completion or iOS throttles further deliveries.
actor BackgroundDeliveryController {
    private let store: HKHealthStore
    private var queries: [HKObserverQuery] = []

    init(store: HKHealthStore) {
        self.store = store
    }

    private var observedTypes: [HKSampleType] {
        HealthMetric.allCases.map(\.quantityType) + [HKObjectType.workoutType()]
    }

    func start(onUpdate: @escaping @Sendable () async -> Void) async {
        guard queries.isEmpty, HealthKitReader.isAvailable else { return }
        for type in observedTypes {
            let query = HKObserverQuery(sampleType: type, predicate: nil) { _, completion, _ in
                Task {
                    await onUpdate()
                    completion()
                }
            }
            store.execute(query)
            queries.append(query)
            try? await store.enableBackgroundDelivery(for: type, frequency: .hourly)
        }
    }

    func stop() async {
        for query in queries {
            store.stop(query)
        }
        queries.removeAll()
        try? await store.disableAllBackgroundDelivery()
    }
}
