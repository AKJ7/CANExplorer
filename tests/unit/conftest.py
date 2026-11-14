import os
import signal
from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
import pytest
from PyQt6.QtCore import QSettings
from pytestqt.qt_compat import qt_api

# Qt must not need a display in tests or CI
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'


@pytest.fixture(scope='session')
def qapp_cls():
    class QTestApplication(qt_api.QtWidgets.QApplication):
        def __init__(self, *args):
            self._register_signals()
            super().__init__(*args)

        def _register_signals(self):
            signal.signal(signal.SIGINT, signal.SIG_DFL)

    return QTestApplication


@pytest.fixture(scope='session')
def data_dir() -> Path:
    return DATA_DIR


E2E_PACKAGE = """    <AR-PACKAGE>
      <SHORT-NAME>E2E</SHORT-NAME>
      <ELEMENTS>
        <END-TO-END-PROTECTION-SET>
          <SHORT-NAME>Protections</SHORT-NAME>
          <END-TO-END-PROTECTIONS>
            <END-TO-END-PROTECTION>
              <SHORT-NAME>EngineProtection</SHORT-NAME>
              <END-TO-END-PROFILE>
                <CATEGORY>PROFILE_05</CATEGORY>
                <DATA-IDS>
                  <DATA-ID>4660</DATA-ID>
                </DATA-IDS>
              </END-TO-END-PROFILE>
              <END-TO-END-PROTECTION-I-SIGNAL-I-PDUS>
                <END-TO-END-PROTECTION-I-SIGNAL-I-PDU>
                  <I-SIGNAL-I-PDU-REF DEST="I-SIGNAL-I-PDU">/ISignalIPdu/EnginePdu</I-SIGNAL-I-PDU-REF>
                </END-TO-END-PROTECTION-I-SIGNAL-I-PDU>
              </END-TO-END-PROTECTION-I-SIGNAL-I-PDUS>
            </END-TO-END-PROTECTION>
          </END-TO-END-PROTECTIONS>
        </END-TO-END-PROTECTION-SET>
      </ELEMENTS>
    </AR-PACKAGE>
"""


@pytest.fixture
def e2e_arxml(data_dir, tmp_path):
    """example.arxml with an END-TO-END-PROTECTION-SET protecting EnginePdu (frame EngineData) with profile 5."""
    text = (data_dir / 'example.arxml').read_text(encoding='utf-8')
    end = text.rindex('  </AR-PACKAGES>')
    path = tmp_path / 'e2e.arxml'
    path.write_text(text[:end] + E2E_PACKAGE + text[end:], encoding='utf-8')
    return path


SECOC_FRAME_TRIGGERING = """                    <CAN-FRAME-TRIGGERING>
                      <SHORT-NAME>SecuredStatus_triggering</SHORT-NAME>
                      <FRAME-REF DEST="CAN-FRAME">/CanFrame/SecuredStatus</FRAME-REF>
                      <CAN-ADDRESSING-MODE>STANDARD</CAN-ADDRESSING-MODE>
                      <IDENTIFIER>768</IDENTIFIER>
                    </CAN-FRAME-TRIGGERING>
"""
SECOC_PDU_TRIGGERINGS = """                    <PDU-TRIGGERING>
                      <SHORT-NAME>StatusPayload_triggering</SHORT-NAME>
                      <I-PDU-REF DEST="I-SIGNAL-I-PDU">/ISignalIPdu/StatusPdu</I-PDU-REF>
                    </PDU-TRIGGERING>
                    <PDU-TRIGGERING>
                      <SHORT-NAME>SecuredStatus_pdu_triggering</SHORT-NAME>
                      <I-PDU-REF DEST="SECURED-I-PDU">/SecuredIPdu/SecuredStatus</I-PDU-REF>
                    </PDU-TRIGGERING>
"""
SECOC_FRAME = """        <CAN-FRAME>
          <SHORT-NAME>SecuredStatus</SHORT-NAME>
          <FRAME-LENGTH>6</FRAME-LENGTH>
          <PDU-TO-FRAME-MAPPINGS>
            <PDU-TO-FRAME-MAPPING>
              <SHORT-NAME>SecuredStatus_mapping</SHORT-NAME>
              <PDU-REF DEST="SECURED-I-PDU">/SecuredIPdu/SecuredStatus</PDU-REF>
              <START-POSITION>0</START-POSITION>
            </PDU-TO-FRAME-MAPPING>
          </PDU-TO-FRAME-MAPPINGS>
        </CAN-FRAME>
"""
SECOC_PACKAGES = """    <AR-PACKAGE>
      <SHORT-NAME>SecuredIPdu</SHORT-NAME>
      <ELEMENTS>
        <SECURED-I-PDU>
          <SHORT-NAME>SecuredStatus</SHORT-NAME>
          <LENGTH>6</LENGTH>
          <AUTHENTICATION-PROPS-REF DEST="SECURE-COMMUNICATION-AUTHENTICATION-PROPS">/SecOC/CMAC_AES128</AUTHENTICATION-PROPS-REF>
          <FRESHNESS-PROPS-REF DEST="SECURE-COMMUNICATION-FRESHNESS-PROPS">/SecOC/FreshnessCounter</FRESHNESS-PROPS-REF>
          <PAYLOAD-REF DEST="PDU-TRIGGERING">/Cluster/Powertrain/PowertrainChannel/StatusPayload_triggering</PAYLOAD-REF>
          <SECURE-COMMUNICATION-PROPS>
            <DATA-ID>18</DATA-ID>
          </SECURE-COMMUNICATION-PROPS>
        </SECURED-I-PDU>
      </ELEMENTS>
    </AR-PACKAGE>
    <AR-PACKAGE>
      <SHORT-NAME>SecOC</SHORT-NAME>
      <ELEMENTS>
        <SECURE-COMMUNICATION-AUTHENTICATION-PROPS>
          <SHORT-NAME>CMAC_AES128</SHORT-NAME>
          <AUTH-INFO-TX-LENGTH>28</AUTH-INFO-TX-LENGTH>
        </SECURE-COMMUNICATION-AUTHENTICATION-PROPS>
        <SECURE-COMMUNICATION-FRESHNESS-PROPS>
          <SHORT-NAME>FreshnessCounter</SHORT-NAME>
          <FRESHNESS-VALUE-LENGTH>32</FRESHNESS-VALUE-LENGTH>
          <FRESHNESS-VALUE-TX-LENGTH>4</FRESHNESS-VALUE-TX-LENGTH>
        </SECURE-COMMUNICATION-FRESHNESS-PROPS>
      </ELEMENTS>
    </AR-PACKAGE>
"""


@pytest.fixture
def secoc_arxml(data_dir, tmp_path):
    """example.arxml with a SECURED-I-PDU SecuredStatus (frame 0x300 on Powertrain): StatusPdu's 2 payload bytes,
    Data ID 18, CMAC_AES128 with a 28-bit authenticator, a 32-bit freshness counter of which 4 bits are sent."""
    text = (data_dir / 'example.arxml').read_text(encoding='utf-8')
    triggerings_end = '                  </FRAME-TRIGGERINGS>\n'
    text = text.replace(triggerings_end, SECOC_FRAME_TRIGGERING + triggerings_end, 1)
    pdu_triggerings_end = '                  </PDU-TRIGGERINGS>\n'
    text = text.replace(pdu_triggerings_end, SECOC_PDU_TRIGGERINGS + pdu_triggerings_end, 1)
    frames = '      <SHORT-NAME>CanFrame</SHORT-NAME>\n      <ELEMENTS>\n'
    text = text.replace(frames, frames + SECOC_FRAME, 1)
    end = text.rindex('  </AR-PACKAGES>')
    path = tmp_path / 'secoc.arxml'
    path.write_text(text[:end] + SECOC_PACKAGES + text[end:], encoding='utf-8')
    return path


@pytest.fixture(scope='session', autouse=True)
def isolated_settings(tmp_path_factory):
    """Keep tests from reading or writing the user's real application settings."""
    path = str(tmp_path_factory.mktemp('settings'))
    for settings_format in (QSettings.Format.NativeFormat, QSettings.Format.IniFormat):
        QSettings.setPath(settings_format, QSettings.Scope.UserScope, path)
    os.environ['CANEXPLORER_KEYSTORE'] = os.path.join(path, 'keystore.json')  # Nor the user's key store
    os.environ.pop('CANEXPLORER_KEYSTORE_PASSWORD', None)
    keyring.set_keyring(MemoryKeyring())  # Nor the OS keychain
    return path


class MemoryKeyring(keyring.backend.KeyringBackend):
    """An in-memory keychain for the tests (the real one may prompt, or hold the user's passwords)."""

    priority = 1

    def __init__(self):
        super().__init__()
        self.passwords = {}

    def get_password(self, service, username):
        return self.passwords.get((service, username))

    def set_password(self, service, username, password):
        self.passwords[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.passwords:
            raise keyring.errors.PasswordDeleteError('not found')
        del self.passwords[(service, username)]


@pytest.fixture
def mqtt_broker():
    """An MQTT broker on a free local port (see mqtt_broker.py); skips tests without the MQTT client."""
    pytest.importorskip('aiomqtt')
    from mqtt_broker import MqttBroker

    broker = MqttBroker()
    broker.start()
    yield broker
    broker.stop()


@pytest.fixture
def secoc_container_arxml(secoc_arxml, tmp_path):
    """secoc_arxml with the secured PDU SecuredStatus also contained in the Bundle container frame (0x200)."""
    text = secoc_arxml.read_text(encoding='utf-8')
    status = (
        '<CONTAINED-PDU-TRIGGERING-REF DEST="PDU-TRIGGERING">/Cluster/Powertrain/PowertrainChannel/StatusPdu_triggering'
        '</CONTAINED-PDU-TRIGGERING-REF>'
    )
    text = text.replace(
        status,
        status
        + '\n            <CONTAINED-PDU-TRIGGERING-REF DEST="PDU-TRIGGERING">'
        + '/Cluster/Powertrain/PowertrainChannel/SecuredStatus_pdu_triggering</CONTAINED-PDU-TRIGGERING-REF>',
        1,
    )
    secured = '<SHORT-NAME>SecuredStatus</SHORT-NAME>\n          <LENGTH>6</LENGTH>'
    text = text.replace(
        secured,
        secured
        + '\n          <CONTAINED-I-PDU-PROPS>\n            <HEADER-ID-SHORT-HEADER>0x0D0E0F</HEADER-ID-SHORT-HEADER>'
        '\n          </CONTAINED-I-PDU-PROPS>',
        1,
    )
    path = tmp_path / 'secoc_container.arxml'
    path.write_text(text, encoding='utf-8')
    return path
