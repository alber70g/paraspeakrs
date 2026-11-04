import Foundation
import Synchronization

/// Single-producer, single-consumer byte ring carrying one record per IO
/// buffer: a header (host time, frame count, sample rate) followed by the
/// mono samples. The IO thread only copies into it; everything that can take
/// time, such as writing minutes of silence after a stall, happens on the
/// consumer side.
final class RecordRing {
    struct Header {
        var hostTime: UInt64
        var frames: UInt32
        var rate: UInt32
    }

    static let headerSize = MemoryLayout<Header>.size
    private let capacity: Int
    private let storage: UnsafeMutableRawPointer
    private let head = Atomic<Int>(0)  // total bytes written
    private let tail = Atomic<Int>(0)  // total bytes read
    private let overflows = Atomic<Int>(0)

    init(capacity: Int) {
        self.capacity = capacity
        storage = .allocate(byteCount: capacity, alignment: 16)
    }

    deinit { storage.deallocate() }

    /// Buffers dropped because the consumer fell behind.
    var droppedBuffers: Int { overflows.load(ordering: .relaxed) }

    /// Producer side. Copies channel 0 of an interleaved buffer; returns its peak.
    func push(_ data: UnsafePointer<Float>, channels: Int, frames: Int, rate: Double, hostTime: UInt64) -> Float {
        let size = Self.headerSize + frames * 4
        let start = head.load(ordering: .relaxed)
        guard capacity - (start - tail.load(ordering: .acquiring)) >= size else {
            overflows.add(1, ordering: .relaxed)
            return 0
        }
        var header = Header(hostTime: hostTime, frames: UInt32(frames), rate: UInt32(rate))
        copyIn(&header, count: Self.headerSize, at: start)
        var peak: Float = 0
        var position = start + Self.headerSize
        for i in 0..<frames {
            var sample = data[i * channels]
            peak = max(peak, abs(sample))
            copyIn(&sample, count: 4, at: position)
            position += 4
        }
        head.store(start + size, ordering: .releasing)
        return peak
    }

    /// Consumer side. Calls `body` for each complete record, oldest first.
    func drain(into scratch: inout [Float], _ body: (Header, UnsafeBufferPointer<Float>) -> Void) {
        var position = tail.load(ordering: .relaxed)
        let end = head.load(ordering: .acquiring)
        while end - position >= Self.headerSize {
            var header = Header(hostTime: 0, frames: 0, rate: 0)
            copyOut(&header, count: Self.headerSize, at: position)
            let frames = Int(header.frames)
            if scratch.count < frames { scratch = [Float](repeating: 0, count: frames) }
            scratch.withUnsafeMutableBytes { copyOut($0.baseAddress!, count: frames * 4, at: position + Self.headerSize) }
            scratch.withUnsafeBufferPointer { body(header, UnsafeBufferPointer(rebasing: $0[0..<frames])) }
            position += Self.headerSize + frames * 4
        }
        tail.store(position, ordering: .releasing)
    }

    private func copyIn(_ source: UnsafeRawPointer, count: Int, at position: Int) {
        let offset = position % capacity
        let first = min(count, capacity - offset)
        (storage + offset).copyMemory(from: source, byteCount: first)
        if first < count { storage.copyMemory(from: source + first, byteCount: count - first) }
    }

    private func copyOut(_ destination: UnsafeMutableRawPointer, count: Int, at position: Int) {
        let offset = position % capacity
        let first = min(count, capacity - offset)
        destination.copyMemory(from: storage + offset, byteCount: first)
        if first < count { (destination + first).copyMemory(from: storage, byteCount: count - first) }
    }
}
