import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtMultimedia
import CanExplorer 1.0

// Camera video and GPS map. Live: the camera preview while recording with a camera. Replay: the recorded video
// and the GPS track, following the replay position and speed.
ColumnLayout {
    id: root
    required property var connection
    readonly property var media: connection.media
    readonly property var replay: connection.replay
    readonly property var appState: connectionManager.app_state
    property string message: ""

    spacing: Theme.spacing

    // A time offset in seconds, nudged in steps of 0.1 s
    component OffsetField: RowLayout {
        id: offsetField
        property real value: 0
        signal edited(real value)
        spacing: 4
        Button {
            text: "−"
            Layout.preferredWidth: 32
            onClicked: offsetField.edited(Math.round((offsetField.value - 0.1) * 1000) / 1000)
        }
        TextField {
            text: offsetField.value.toFixed(2)
            horizontalAlignment: Text.AlignRight
            validator: DoubleValidator { decimals: 3; notation: DoubleValidator.StandardNotation }
            Layout.preferredWidth: 80
            onEditingFinished: offsetField.edited(Number(text) || 0)
        }
        Button {
            text: "+"
            Layout.preferredWidth: 32
            onClicked: offsetField.edited(Math.round((offsetField.value + 0.1) * 1000) / 1000)
        }
        Label { text: qsTr("s"); color: Theme.muted }
    }

    FileChooser {
        id: gpsDialog
        title: qsTr("Attach a GPS track")
        nameFilters: [qsTr("GPS tracks (*.gpx *.nmea *.txt *.log)"), qsTr("All files (*)")]
        onAccepted: root.message = root.media.attach_gps(selectedFile)
    }

    // Live connection: camera preview
    Card {
        visible: !root.media.is_replay
        title: qsTr("Camera")
        Layout.fillWidth: true
        Layout.fillHeight: true
        ColumnLayout {
            anchors.fill: parent
            spacing: 8
            Label {
                text: root.media.camera_recording ? root.media.camera_status
                    : (root.media.camera_status || qsTr("Choose a camera in the Record dialog to record a video in step with the CAN log. "
                        + "Replay the log to see the video and attach a GPS track."))
                color: root.media.camera_recording ? Theme.text : Theme.muted
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            VideoOutput {
                id: cameraPreview
                visible: root.media.camera_recording
                fillMode: VideoOutput.PreserveAspectFit
                Layout.fillWidth: true
                Layout.fillHeight: true
                Component.onCompleted: root.media.set_preview(cameraPreview)
                Component.onDestruction: root.media.set_preview(null)
            }
            Item { visible: !root.media.camera_recording; Layout.fillHeight: true }
        }
    }

    // Replay: video and map
    RowLayout {
        visible: root.media.is_replay
        spacing: Theme.spacing
        Layout.fillWidth: true
        Layout.fillHeight: true

        Card {
            title: root.media.video_name ? qsTr("Video: %1").arg(root.media.video_name) : qsTr("Video")
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 1
            ColumnLayout {
                anchors.fill: parent
                spacing: 8
                Item {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Rectangle { anchors.fill: parent; color: "black"; visible: root.media.video_url.length > 0 }
                    VideoOutput {
                        id: videoOutput
                        anchors.fill: parent
                        fillMode: VideoOutput.PreserveAspectFit
                    }
                    Label {
                        anchors.centerIn: parent
                        width: parent.width - 32
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.Wrap
                        visible: text.length > 0
                        color: root.media.video_url.length > 0 ? "white" : Theme.muted
                        text: root.media.video_problem ? root.media.video_problem
                            : !root.media.video_url ? qsTr("No video was recorded with this log")
                            : root.media.video_target < 0 || (player.duration > 0 && root.media.video_target * 1000 > player.duration)
                                ? qsTr("No video at this time") : ""
                    }
                }
                RowLayout {
                    visible: root.media.video_url.length > 0
                    spacing: 8
                    Label { text: qsTr("Video offset"); color: Theme.muted }
                    OffsetField {
                        value: root.media.video_offset
                        onEdited: value => root.message = root.media.set_video_offset(value)
                    }
                    Label {
                        text: qsTr("positive shows later video")
                        color: Theme.muted
                        font.pixelSize: 11
                    }
                    Item { Layout.fillWidth: true }
                }
            }
        }

        Card {
            title: root.media.track_name ? qsTr("GPS: %1").arg(root.media.track_name) : qsTr("GPS")
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 1
            headerItems: [
                Button {
                    text: root.media.has_track ? qsTr("Replace track…") : qsTr("Attach GPS track…")
                    onClicked: gpsDialog.open()
                },
                Button {
                    visible: root.media.has_track
                    text: qsTr("Remove")
                    onClicked: root.media.detach_gps()
                }
            ]
            ColumnLayout {
                anchors.fill: parent
                spacing: 8
                GpsMap {
                    visible: root.media.has_track
                    media: root.media
                    tilesEnabled: root.appState.map_tiles
                    tileUrl: root.appState.map_tile_url
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                }
                Label {
                    visible: !root.media.has_track
                    text: root.media.track_error || qsTr("Attach a GPX file or an NMEA 0183 log to see where the recording was made. "
                        + "Tracks with dates are aligned by time; others start with the log.")
                    color: root.media.track_error ? Theme.danger : Theme.muted
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    verticalAlignment: Text.AlignVCenter
                    horizontalAlignment: Text.AlignHCenter
                }
                Label {
                    visible: root.media.has_track
                    readonly property var current: root.media.current
                    text: current.valid
                        ? qsTr("%1, %2").arg(current.lat.toFixed(6)).arg(current.lon.toFixed(6))
                            + (current.speed_kmh >= 0 ? "  ·  " + qsTr("%1 km/h").arg(current.speed_kmh.toFixed(1)) : "")
                            + (isNaN(current.altitude) ? "" : "  ·  " + qsTr("%1 m").arg(current.altitude.toFixed(0)))
                        : qsTr("No GPS fix at this time")
                    color: current.valid ? Theme.text : Theme.muted
                    font.family: "monospace"
                }
                Label {
                    visible: root.media.has_track
                    text: root.media.track_summary
                    color: Theme.muted
                    font.pixelSize: 11
                }
                RowLayout {
                    visible: root.media.has_track
                    spacing: 8
                    Label { text: qsTr("GPS offset"); color: Theme.muted }
                    OffsetField {
                        value: root.media.gps_offset
                        onEdited: value => root.message = root.media.set_gps_offset(value)
                    }
                    Item { Layout.fillWidth: true }
                }
                Label {
                    visible: root.media.has_track && !root.appState.map_tiles
                    text: qsTr("Map tiles are off: enable them in Preferences to show the track on a map.")
                    color: Theme.muted
                    font.pixelSize: 11
                }
            }
        }
    }

    Label {
        visible: root.message.length > 0
        text: root.message
        color: Theme.danger
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    // Keeps the video in step with the replay: plays at the replay speed and seeks when it drifts
    MediaPlayer {
        id: player
        source: root.media.is_replay ? root.media.video_url : ""
        videoOutput: videoOutput
        onMediaStatusChanged: root.syncVideo()
    }

    function syncVideo() {
        if (!root.media.video_url || player.mediaStatus === MediaPlayer.NoMedia
                || player.mediaStatus === MediaPlayer.LoadingMedia || player.mediaStatus === MediaPlayer.InvalidMedia)
            return
        const target = root.media.video_target * 1000
        const inside = target >= 0 && (player.duration <= 0 || target <= player.duration)
        const playing = inside && root.replay.state === "playing" && !root.replay.paused && root.replay.speed > 0
        if (playing) {
            if (player.playbackRate !== root.replay.speed)
                player.playbackRate = root.replay.speed
            if (player.playbackState !== MediaPlayer.PlayingState)
                player.play()
        } else if (player.playbackState === MediaPlayer.PlayingState) {
            player.pause()
        }
        if (!inside)
            return
        // While playing, small drifts are left alone (seeking stutters); when paused, show the exact frame
        if (Math.abs(player.position - target) > (playing ? 400 : 40))
            player.setPosition(target)
    }

    Connections {
        target: root.media
        function onPosition_changed() { root.syncVideo() }
    }
    Connections {
        target: root.replay
        function onChanged() { root.syncVideo() }
    }
}
