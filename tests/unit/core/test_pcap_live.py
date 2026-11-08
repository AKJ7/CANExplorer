"""Tests for the libpcap capture wrapper (can_explorer.core.pcap_live).

The capture *read loop* is tested by reading a pcap savefile through the very same libpcap path a live capture
uses (pcap_open_offline + pcap_next_ex), which needs no capture privileges. Live capture on a real interface
additionally needs root/CAP_NET_RAW and so is not exercised here. Tests needing the library are skipped when it is
not installed, so the suite still passes on a machine without libpcap/Npcap."""

import pytest

from can_explorer.core import ethernet, pcap_live

needs_libpcap = pytest.mark.skipif(not pcap_live.available(), reason='libpcap/Npcap not installed')


def test_available_returns_bool():
    assert isinstance(pcap_live.available(), bool)


def test_list_interfaces_is_always_a_list():
    assert isinstance(pcap_live.list_interfaces(), list)


@needs_libpcap
def test_list_interfaces_are_strings():
    names = pcap_live.list_interfaces()
    assert all(isinstance(name, str) for name in names)


@needs_libpcap
def test_read_offline_round_trip(tmp_path):
    # Write a classic pcap with the tool's own writer, then read it back through libpcap: this checks the
    # pcap_pkthdr marshalling (timestamp and length) and the packet-bytes copy of the shared read loop.
    frames = [ethernet.RawPacket(1.500000, b'\xaa' * 20), ethernet.RawPacket(1.750000, b'\xbb' * 14)]
    path = tmp_path / 'capture.pcap'
    ethernet.write_pcap(str(path), frames)

    got = []
    pcap_live.read_offline(str(path), lambda timestamp, data: got.append((timestamp, data)))
    assert len(got) == 2
    assert got[0][1] == b'\xaa' * 20 and got[1][1] == b'\xbb' * 14
    assert abs(got[0][0] - 1.5) < 1e-6 and abs(got[1][0] - 1.75) < 1e-6


@needs_libpcap
def test_read_offline_rejects_missing_file(tmp_path):
    with pytest.raises(pcap_live.PcapError):
        pcap_live.read_offline(str(tmp_path / 'does-not-exist.pcap'), lambda timestamp, data: None)


@needs_libpcap
def test_capture_start_errors_on_unknown_interface():
    capture = pcap_live.PcapCapture('no-such-interface-zzz', lambda timestamp, data: None)
    with pytest.raises(pcap_live.PcapError):
        capture.start()


def test_unavailable_calls_raise(monkeypatch):
    # Force the "no libpcap" path and confirm the live entry points raise a clear error rather than crashing.
    monkeypatch.setattr(pcap_live, '_lib', None)
    assert pcap_live.list_interfaces() == []
    with pytest.raises(pcap_live.PcapError, match='libpcap is not available'):
        pcap_live.read_offline('whatever.pcap', lambda timestamp, data: None)
    with pytest.raises(pcap_live.PcapError, match='libpcap is not available'):
        pcap_live.PcapCapture('eth0', lambda timestamp, data: None).start()
