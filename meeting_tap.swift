// meeting_tap.swift — capture system-output audio (far side) + microphone (near
// side) into one 16 kHz mono WAV, WITHOUT redirecting the user's output.
//
// Uses a Core Audio *process tap* (macOS 14.2+). The tap is created UNMUTED, so
// system audio keeps playing normally through whatever the user is listening on
// (speakers / headphones / whatever they hot-plug) and volume keys keep working.
// We combine the tap and the default input device in a private aggregate device
// and sum every channel down to mono.
//
//   meeting_tap /path/to/out.wav
//
// Runs until SIGTERM / SIGINT, then finalizes the file and cleans up the tap and
// aggregate device. Prints "READY" on stdout once audio is actually flowing.
//
// Build:  swiftc -O meeting_tap.swift -o meeting_tap \
//               -framework CoreAudio -framework AudioToolbox -framework Foundation

import Foundation
import CoreAudio
import AudioToolbox
import Darwin

// ---------------------------------------------------------- signal handling ---
nonisolated(unsafe) var gStop: sig_atomic_t = 0
func onSignal(_ s: Int32) { gStop = 1 }

// ----------------------------------------------------------------- utilities ---
func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(1)
}

func check(_ status: OSStatus, _ what: String) {
    if status != noErr { fail("CoreAudio error \(status) during \(what)") }
}

let debug = ProcessInfo.processInfo.environment["TAP_DEBUG"] != nil
func step(_ s: String) {
    if debug { FileHandle.standardError.write(("[step] " + s + "\n").data(using: .utf8)!) }
}

func stringProperty(_ obj: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var addr = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<CFString?>.size)
    var value: CFString? = nil
    let st = withUnsafeMutablePointer(to: &value) {
        AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, $0)
    }
    if st != noErr { return nil }
    return value as String?
}

func defaultInputDevice() -> AudioObjectID {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultInputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var dev = AudioObjectID(0)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    check(AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                     &addr, 0, nil, &size, &dev),
          "get default input device")
    return dev
}

func nominalSampleRate(_ dev: AudioObjectID) -> Double {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyNominalSampleRate,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var rate = Double(48000)
    var size = UInt32(MemoryLayout<Double>.size)
    _ = AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &rate)
    return rate > 0 ? rate : 48000
}

// ---------------------------------------------------------------- arguments ---
guard CommandLine.arguments.count >= 2 else { fail("usage: meeting_tap <out.wav>") }
let outPath = CommandLine.arguments[1]

// Install signal handlers first so a hang can always be killed cleanly.
signal(SIGTERM, onSignal)
signal(SIGINT, onSignal)
step("start")

// ------------------------------------------------------------ 1) create tap ---
// Mono mix of the entire system output, excluding no processes = capture all.
// Unmuted so the user still hears everything at normal volume.
let tapDesc = CATapDescription(monoGlobalTapButExcludeProcesses: [])
tapDesc.name = "MeetingTap"
tapDesc.muteBehavior = .unmuted
tapDesc.isPrivate = true

var tapID = AudioObjectID(kAudioObjectUnknown)
step("creating process tap")
check(AudioHardwareCreateProcessTap(tapDesc, &tapID), "create process tap")
step("tap created id=\(tapID)")
guard let tapUID = stringProperty(tapID, kAudioTapPropertyUID) else {
    fail("could not read tap UID")
}
step("tap uid=\(tapUID)")

// ------------------------------------------------- 2) create aggregate device ---
let micID = defaultInputDevice()
let micUID = stringProperty(micID, kAudioDevicePropertyDeviceUID) ?? ""

let aggUID = "com.meetingrecorder.tap.aggregate"
let description: [String: Any] = [
    kAudioAggregateDeviceNameKey as String: "MeetingRecorderTap",
    kAudioAggregateDeviceUIDKey as String: aggUID,
    kAudioAggregateDeviceIsPrivateKey as String: true,
    kAudioAggregateDeviceIsStackedKey as String: false,
    kAudioAggregateDeviceMainSubDeviceKey as String: micUID,
    kAudioAggregateDeviceSubDeviceListKey as String: [
        [kAudioSubDeviceUIDKey as String: micUID],
    ],
    kAudioAggregateDeviceTapListKey as String: [
        [
            kAudioSubTapDriftCompensationKey as String: true,
            kAudioSubTapUIDKey as String: tapUID,
        ],
    ],
]

step("mic uid=\(micUID)")
var aggID = AudioObjectID(kAudioObjectUnknown)
step("creating aggregate")
check(AudioHardwareCreateAggregateDevice(description as CFDictionary, &aggID),
      "create aggregate device")
step("aggregate created id=\(aggID)")

let deviceRate = nominalSampleRate(aggID)
step("device rate=\(deviceRate)")

// ------------------------------------------------------ 3) open the WAV file ---
// File: 16 kHz, mono, 16-bit PCM. Client: Float32 mono @ deviceRate (ExtAudioFile
// resamples on write).
var fileFormat = AudioStreamBasicDescription(
    mSampleRate: 16000,
    mFormatID: kAudioFormatLinearPCM,
    mFormatFlags: kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked,
    mBytesPerPacket: 2, mFramesPerPacket: 1, mBytesPerFrame: 2,
    mChannelsPerFrame: 1, mBitsPerChannel: 16, mReserved: 0)

var clientFormat = AudioStreamBasicDescription(
    mSampleRate: deviceRate,
    mFormatID: kAudioFormatLinearPCM,
    mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
    mBytesPerPacket: 4, mFramesPerPacket: 1, mBytesPerFrame: 4,
    mChannelsPerFrame: 1, mBitsPerChannel: 32, mReserved: 0)

let url = URL(fileURLWithPath: outPath) as CFURL
var extFile: ExtAudioFileRef? = nil
check(ExtAudioFileCreateWithURL(url, kAudioFileWAVEType, &fileFormat,
                                nil, AudioFileFlags.eraseFile.rawValue, &extFile),
      "create WAV")
check(ExtAudioFileSetProperty(extFile!, kExtAudioFileProperty_ClientDataFormat,
                              UInt32(MemoryLayout<AudioStreamBasicDescription>.size),
                              &clientFormat),
      "set client format")
let file = extFile!

// Shared scratch buffer, guarded because the IOProc runs on a realtime thread.
final class WriteState {
    var mono = [Float](repeating: 0, count: 8192)
    let lock = NSLock()
    var finished = false
}
let state = WriteState()

// ---------------------------------------------------------- 4) IOProc block ---
var procID: AudioDeviceIOProcID? = nil
let ioBlock: AudioDeviceIOBlock = { (_, inInputData, _, _, _) in
    let abl = UnsafeMutableAudioBufferListPointer(
        UnsafeMutablePointer(mutating: inInputData))
    guard abl.count > 0 else { return }

    let first = abl[0]
    let chans0 = max(1, Int(first.mNumberChannels))
    let frames = Int(first.mDataByteSize) / (MemoryLayout<Float>.size * chans0)
    if frames <= 0 { return }

    state.lock.lock()
    if state.finished { state.lock.unlock(); return }
    if state.mono.count < frames { state.mono = [Float](repeating: 0, count: frames) }
    for i in 0..<frames { state.mono[i] = 0 }

    // Sum every channel of every buffer (mic + tap) into mono.
    for buf in abl {
        let nch = Int(buf.mNumberChannels)
        guard nch > 0, let raw = buf.mData else { continue }
        let ptr = raw.assumingMemoryBound(to: Float.self)
        for f in 0..<frames {
            var acc: Float = 0
            for c in 0..<nch { acc += ptr[f * nch + c] }
            state.mono[f] += acc
        }
    }
    for i in 0..<frames {
        let v = state.mono[i]
        state.mono[i] = v > 1 ? 1 : (v < -1 ? -1 : v)
    }

    state.mono.withUnsafeMutableBufferPointer { mp in
        var outABL = AudioBufferList(
            mNumberBuffers: 1,
            mBuffers: AudioBuffer(
                mNumberChannels: 1,
                mDataByteSize: UInt32(frames * MemoryLayout<Float>.size),
                mData: UnsafeMutableRawPointer(mp.baseAddress)))
        _ = ExtAudioFileWrite(file, UInt32(frames), &outABL)
    }
    state.lock.unlock()
}

let queue = DispatchQueue(label: "meeting_tap.io")
step("creating IOProc")
check(AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, queue, ioBlock),
      "create IOProc")
step("starting device")
check(AudioDeviceStart(aggID, procID), "start device")
step("device started")

// Tell the parent we are live.
FileHandle.standardOutput.write("READY\n".data(using: .utf8)!)

// --------------------------------------------------------- 5) run & teardown ---
while gStop == 0 { Thread.sleep(forTimeInterval: 0.1) }
step("stopping")

state.lock.lock(); state.finished = true; state.lock.unlock()
AudioDeviceStop(aggID, procID)
if let p = procID { AudioDeviceDestroyIOProcID(aggID, p) }
ExtAudioFileDispose(file)
AudioHardwareDestroyAggregateDevice(aggID)
AudioHardwareDestroyProcessTap(tapID)
exit(0)
