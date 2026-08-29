import Foundation
import HealthKit

@MainActor
final class SyncCoordinator: ObservableObject {
    enum State: Equatable {
        case idle
        case authorizing
        case syncing
        case failed(String)
    }

    @Published private(set) var state: State = .idle
    @Published private(set) var isAuthorized = false
    @Published private(set) var lastSyncedAt: Date?
    @Published private(set) var pending: [PipelineRecord] = []
    @Published private(set) var recentSamples: [HealthSampleRecord] = []
    @Published private(set) var recentWorkouts: [WorkoutSessionRecord] = []

    private let store = HKHealthStore()
    private let reader: HealthKitReader
    private let uploader = HealthUploader()
    private let settings: SyncSettings
    private lazy var observer = BackgroundDeliveryController(store: store)

    /// First run has no anchor, so it backfills this far and then goes incremental.
    private let backfill = TimeInterval(30 * 24 * 60 * 60)

    init(settings: SyncSettings) {
        self.settings = settings
        self.reader = HealthKitReader(store: store)
    }

    var isHealthDataAvailable: Bool { HealthKitReader.isAvailable }

    func refreshAuthorization() async {
        isAuthorized = await reader.hasRequestedAuthorization()
    }

    func requestAuthorization() async {
        state = .authorizing
        do {
            try await reader.requestAuthorization()
            isAuthorized = true
            state = .idle
            await sync()
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    func sync() async {
        guard state != .syncing else { return }
        state = .syncing
        do {
            let since = lastSyncedAt == nil ? Date().addingTimeInterval(-backfill) : nil
            var samples: [HealthSampleRecord] = []
            for metric in HealthMetric.allCases {
                samples.append(contentsOf: try await reader.samples(for: metric, since: since))
            }
            let workouts = try await reader.workouts(since: since)

            recentSamples = (samples + recentSamples).sorted { $0.start > $1.start }
            recentWorkouts = (workouts + recentWorkouts).sorted { $0.start > $1.start }
            pending += samples.map(PipelineRecord.sample) + workouts.map(PipelineRecord.workout)

            try await flush()
            lastSyncedAt = Date()
            state = .idle
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    /// Records stay in `pending` until the endpoint accepts them, so a failed
    /// or unconfigured upload never silently drops data the anchor already consumed.
    func flush() async throws {
        guard let endpoint = settings.endpointURL, !pending.isEmpty else { return }
        try await uploader.upload(pending, to: endpoint, token: settings.token)
        pending.removeAll()
    }

    func exportPending() throws -> URL {
        try NDJSONExport.write(pending.isEmpty ? snapshotRecords() : pending)
    }

    func applyBackgroundDeliveryPreference() async {
        if settings.backgroundDeliveryEnabled {
            await observer.start { [weak self] in
                await self?.sync()
            }
        } else {
            await observer.stop()
        }
    }

    private func snapshotRecords() -> [PipelineRecord] {
        recentSamples.map(PipelineRecord.sample) + recentWorkouts.map(PipelineRecord.workout)
    }
}
