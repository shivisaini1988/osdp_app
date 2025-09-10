import sys
import threading
import time
from typing import Optional
from pathlib import Path
import serial.tools.list_ports

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QGroupBox, QLabel, QLineEdit, QPushButton, QComboBox,
    QTreeWidget, QTreeWidgetItem, QTextEdit, QProgressBar, QSpinBox,
    QCheckBox, QFileDialog, QMessageBox, QFormLayout, QGridLayout,
    QSplitter, QFrame, QDialog
)
from PySide6.QtCore import Qt, QThread, QTimer, Signal, QObject
from PySide6.QtGui import QFont, QIcon

from TKH_OSDP.ControlPanel import ControlPanel
from TKH_OSDP.OsdpDevice import OsdpDevice
from TKH_OSDP.OsdpErrors import OsdpError
from TKH_OSDP.transport.SerialTransport import SerialTransport
from TKH_OSDP.transport.USBTransport import USBTransport
from TKH_OSDP.commands.osdp_idReport import osdp_IdReportCommand
from TKH_OSDP.commands.osdp_Buzzer import osdp_BuzzerCommand
from TKH_OSDP.commands.osdp_LED import osdp_LEDCommand
from TKH_OSDP.commands.osdp_Output import osdp_OutputCommand
from TKH_OSDP.commands.osdp_ISTAT import osdp_ISTATCommand
from TKH_OSDP.commands.osdp_OSTAT import osdp_OSTATCommand
from TKH_OSDP.commands.osdp_LSTAT import osdp_LSTATCommand
from TKH_OSDP.commands.osdp_RSTAT import osdp_RSTATCommand
from TKH_OSDP.commands.osdp_CAP import osdp_CAPCommand
from TKH_OSDP.commands.osdp_POLL import osdp_POLLCommand
from TKH_OSDP.commands.osdp_MFG import osdp_MFGCommand, MFG_COMMANDS
from result import Ok, Err


class SetAddressDialog(QDialog):
    """Dialog for setting device address"""
    def __init__(self, current_address: int, serial_number: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set Device Address")
        self.setModal(True)
        self.setFixedSize(300, 150)
        
        self.current_address = current_address
        self.serial_number = serial_number
        self.new_address = current_address
        
        self.setup_ui()
    
    def setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Device info
        info_label = QLabel(f"Device Serial Number: {self.serial_number}")
        layout.addWidget(info_label)
        
        current_label = QLabel(f"Current Address: {self.current_address}")
        layout.addWidget(current_label)
        
        # Address input
        address_layout = QHBoxLayout()
        address_layout.addWidget(QLabel("New Address:"))
        
        self.address_spin = QSpinBox()
        self.address_spin.setRange(0, 126)  # Valid OSDP address range
        self.address_spin.setValue(self.current_address)
        address_layout.addWidget(self.address_spin)
        
        layout.addLayout(address_layout)
        
        # Buttons
        button_layout = QHBoxLayout()
        
        self.ok_btn = QPushButton("OK")
        self.cancel_btn = QPushButton("Cancel")
        
        self.ok_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        
        button_layout.addWidget(self.ok_btn)
        button_layout.addWidget(self.cancel_btn)
        
        layout.addLayout(button_layout)
    
    def get_new_address(self):
        return self.address_spin.value()


class DeviceDiscoveryWorker(QObject):
    """Worker thread for device discovery"""
    device_found = Signal(int, int, str)  # address, serial_number, status
    discovery_finished = Signal()
    error_occurred = Signal(str)
    
    def __init__(self, control_panel: ControlPanel, num_devices: int):
        super().__init__()
        self.control_panel = control_panel
        self.num_devices = num_devices
        self.running = True
    
    def discover(self):
        try:
            original_device_count = len(self.control_panel.devices)
            self.control_panel.discover(self.num_devices)
            
            # Emit signals for newly discovered devices
            for device in self.control_panel.devices[original_device_count:]:
                self.device_found.emit(
                    device.pd_address, 
                    device.serial_number, 
                    "Discovered"
                )
            
            self.discovery_finished.emit()
        except Exception as e:
            self.error_occurred.emit(str(e))
    
    def stop(self):
        self.running = False


class StatusMonitorWorker(QObject):
    """Worker thread for continuous status monitoring"""
    status_update = Signal(str)
    
    def __init__(self, device: OsdpDevice):
        super().__init__()
        self.device = device
        self.running = True
    
    def monitor(self):
        while self.running:
            try:
                # Poll device
                poll_cmd = osdp_POLLCommand()
                result = self.device.POLL(poll_cmd)
                
                match result:
                    case Ok(response):
                        self.status_update.emit(f"Poll OK - Reply: {hex(response.reply)}")
                    case Err(error):
                        self.status_update.emit(f"Poll Error: {error}")
                
                time.sleep(1)  # Poll every second
                
            except Exception as e:
                self.status_update.emit(f"Monitor Error: {str(e)}")
                break
    
    def stop(self):
        self.running = False


class OSDPMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OSDP Control Panel - PySide6")
        self.setGeometry(100, 100, 1200, 800)
        
        # Initialize variables
        self.control_panel: Optional[ControlPanel] = None
        self.current_device: Optional[OsdpDevice] = None
        self.discovery_worker: Optional[DeviceDiscoveryWorker] = None
        self.discovery_thread: Optional[QThread] = None
        self.monitor_worker: Optional[StatusMonitorWorker] = None
        self.monitor_thread: Optional[QThread] = None
        
        # Default VID/PID for OSDP devices
        self.default_vid = 0x0403  # FTDI VID
        self.default_pid = 0x6001  # FTDI PID
        
        self.setup_ui()
        self.setup_styles()
    
    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        layout = QVBoxLayout(central_widget)
        
        # Create tab widget
        self.tab_widget = QTabWidget()
        layout.addWidget(self.tab_widget)
        
        # Setup tabs - status tab first to initialize status_text, then connection tab as first visible tab
        self.setup_status_tab()
        self.setup_connection_tab()
        self.setup_control_tab()
        self.setup_firmware_tab()
        self.setup_advanced_tab()
        
        # Move connection tab to first position
        connection_widget = self.tab_widget.widget(1)  # Connection tab is at index 1
        self.tab_widget.removeTab(1)
        self.tab_widget.insertTab(0, connection_widget, "Connection")
    
    def setup_styles(self):
        """Apply modern styling"""
        self.setStyleSheet("""
            QMainWindow {
                background-color: #f0f0f0;
            }
            QGroupBox {
                font-weight: bold;
                border: 2px solid #cccccc;
                border-radius: 5px;
                margin-top: 1ex;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
            QPushButton {
                background-color: #4CAF50;
                border: none;
                color: white;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3d8b40;
            }
            QPushButton:disabled {
                background-color: #cccccc;
                color: #666666;
            }
            QLineEdit, QComboBox, QSpinBox {
                padding: 5px;
                border: 1px solid #ddd;
                border-radius: 3px;
            }
            QTreeWidget {
                border: 1px solid #ddd;
                border-radius: 3px;
            }
            QTextEdit {
                border: 1px solid #ddd;
                border-radius: 3px;
                font-family: 'Courier New', monospace;
            }
        """)
    
    def setup_connection_tab(self):
        connection_widget = QWidget()
        self.tab_widget.addTab(connection_widget, "Connection")
        
        layout = QVBoxLayout(connection_widget)
        
        # Transport Settings
        transport_group = QGroupBox("Transport Settings")
        layout.addWidget(transport_group)
        
        transport_layout = QFormLayout(transport_group)
        
        # Transport type
        self.transport_combo = QComboBox()
        self.transport_combo.addItems(["Serial", "USB"])
        transport_layout.addRow("Transport Type:", self.transport_combo)
        
        # VID/PID settings
        vid_pid_layout = QHBoxLayout()
        self.vid_edit = QLineEdit(f"0x{self.default_vid:04X}")
        self.pid_edit = QLineEdit(f"0x{self.default_pid:04X}")
        vid_pid_layout.addWidget(QLabel("VID:"))
        vid_pid_layout.addWidget(self.vid_edit)
        vid_pid_layout.addWidget(QLabel("PID:"))
        vid_pid_layout.addWidget(self.pid_edit)
        transport_layout.addRow("USB VID/PID:", vid_pid_layout)

        # Serial port selection
        port_layout = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.refresh_ports_btn = QPushButton("Refresh Ports")
        self.find_device_btn = QPushButton("Find by VID/PID")
        self.refresh_ports_btn.clicked.connect(self.refresh_serial_ports)
        self.find_device_btn.clicked.connect(self.find_device_by_vid_pid)
        port_layout.addWidget(self.port_combo)
        port_layout.addWidget(self.refresh_ports_btn)
        port_layout.addWidget(self.find_device_btn)
        transport_layout.addRow("Serial Port:", port_layout)

        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems(["9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"])
        self.baud_combo.setCurrentText("115200")
        transport_layout.addRow("Baud Rate:", self.baud_combo)
        
        # Initialize serial ports
        self.refresh_serial_ports()
        
        # Connection buttons
        button_layout = QHBoxLayout()
        self.connect_btn = QPushButton("Connect")
        self.disconnect_btn = QPushButton("Disconnect")
        self.discover_btn = QPushButton("Discover Devices")
        
        self.connect_btn.clicked.connect(self.connect_transport)
        self.disconnect_btn.clicked.connect(self.disconnect_transport)
        self.discover_btn.clicked.connect(self.discover_devices)
        
        self.disconnect_btn.setEnabled(False)
        self.discover_btn.setEnabled(False)
        
        button_layout.addWidget(self.connect_btn)
        button_layout.addWidget(self.disconnect_btn)
        button_layout.addWidget(self.discover_btn)
        button_layout.addStretch()
        
        transport_layout.addRow(button_layout)
        
        # Device List
        device_group = QGroupBox("Discovered Devices")
        layout.addWidget(device_group)
        
        device_layout = QVBoxLayout(device_group)
        
        # Add instruction label
        instruction_label = QLabel("Double-click on a device to set its address")
        instruction_label.setStyleSheet("color: #666666; font-style: italic;")
        device_layout.addWidget(instruction_label)
        
        self.device_tree = QTreeWidget()
        self.device_tree.setHeaderLabels(["Address", "Serial Number", "Status"])
        self.device_tree.itemSelectionChanged.connect(self.on_device_selected)
        self.device_tree.itemDoubleClicked.connect(self.on_device_double_clicked)
        device_layout.addWidget(self.device_tree)
        
        # Security Settings
        security_group = QGroupBox("Security")
        layout.addWidget(security_group)
        
        security_layout = QFormLayout(security_group)
        
        self.key_edit = QLineEdit("303132333435363738393A3B3C3D3E3F")
        security_layout.addRow("SCBK Key:", self.key_edit)
        
        secure_button_layout = QHBoxLayout()
        
        self.secure_btn = QPushButton("Setup Secure Channel")
        self.secure_btn.clicked.connect(self.setup_secure_channel)
        self.secure_btn.setEnabled(False)
        secure_button_layout.addWidget(self.secure_btn)
        
        self.reset_secure_btn = QPushButton("Reset to Unencrypted")
        self.reset_secure_btn.clicked.connect(self.reset_secure_channel)
        self.reset_secure_btn.setEnabled(False)
        secure_button_layout.addWidget(self.reset_secure_btn)
        
        security_layout.addRow(secure_button_layout)
        
        layout.addStretch()
    
    @staticmethod
    def find_device_port(vid: int, pid: int) -> Optional[str]:
        """Find device port by VID/PID"""
        try:
            ports = serial.tools.list_ports.comports()
            for port in ports:
                if port.vid == vid and port.pid == pid:
                    return port.device
            return None
        except Exception:
            return None
    
    def refresh_serial_ports(self):
        """Refresh the list of available serial ports"""
        try:
            # Clear current items
            self.port_combo.clear()
            
            # Get list of available ports
            ports = serial.tools.list_ports.comports()
            
            if ports:
                for port in sorted(ports):
                    # Add port with description and VID/PID info
                    port_info = f"{port.device}"
                    if port.description and port.description != "n/a":
                        port_info += f" - {port.description}"
                    if port.vid and port.pid:
                        port_info += f" (VID:0x{port.vid:04X} PID:0x{port.pid:04X})"
                    elif port.manufacturer and port.manufacturer != "n/a":
                        port_info += f" ({port.manufacturer})"
                    
                    self.port_combo.addItem(port_info, port.device)
                
                self.log_message(f"Found {len(ports)} serial port(s)")
            else:
                # Add some common default ports if none found
                default_ports = ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyACM0", "/dev/ttyACM1", "COM1", "COM2", "COM3", "COM4"]
                for port in default_ports:
                    self.port_combo.addItem(port, port)
                
                self.log_message("No serial ports detected, added default ports")
                
        except Exception as e:
            self.log_message(f"Error refreshing serial ports: {str(e)}")
            # Add fallback default port
            self.port_combo.addItem("/dev/ttyUSB0", "/dev/ttyUSB0")
    
    def find_device_by_vid_pid(self):
        """Find and select device by VID/PID"""
        try:
            # Parse VID/PID from input fields
            vid_text = self.vid_edit.text().strip()
            pid_text = self.pid_edit.text().strip()
            
            # Handle hex format (0x prefix) or decimal
            if vid_text.startswith('0x') or vid_text.startswith('0X'):
                vid = int(vid_text, 16)
            else:
                vid = int(vid_text)
                
            if pid_text.startswith('0x') or pid_text.startswith('0X'):
                pid = int(pid_text, 16)
            else:
                pid = int(pid_text)
            
            # Find device port
            device_port = self.find_device_port(vid, pid)
            
            if device_port:
                self.log_message(f"Device found at port: {device_port} (VID:0x{vid:04X} PID:0x{pid:04X})")
                
                # Refresh ports to make sure we have the latest list
                self.refresh_serial_ports()
                
                # Select the found port in combo box
                for i in range(self.port_combo.count()):
                    if self.port_combo.itemData(i) == device_port:
                        self.port_combo.setCurrentIndex(i)
                        break
                else:
                    # If not found in combo, add it manually
                    port_info = f"{device_port} - Found by VID/PID (VID:0x{vid:04X} PID:0x{pid:04X})"
                    self.port_combo.addItem(port_info, device_port)
                    self.port_combo.setCurrentIndex(self.port_combo.count() - 1)
                
                QMessageBox.information(self, "Device Found", f"Device found at port: {device_port}")
            else:
                self.log_message(f"Device not found with VID:0x{vid:04X} PID:0x{pid:04X}")
                QMessageBox.warning(self, "Device Not Found", f"No device found with VID:0x{vid:04X} PID:0x{pid:04X}")
                
        except ValueError as e:
            QMessageBox.warning(self, "Invalid Input", f"Invalid VID/PID format: {str(e)}")
        except Exception as e:
            QMessageBox.critical(self, "Search Error", f"Error searching for device: {str(e)}")
    
    def setup_control_tab(self):
        control_widget = QWidget()
        self.tab_widget.addTab(control_widget, "Device Control")
        
        layout = QVBoxLayout(control_widget)
        
        # LED Control
        led_group = QGroupBox("LED Control")
        layout.addWidget(led_group)
        
        led_layout = QGridLayout(led_group)
        
        led_layout.addWidget(QLabel("Reader:"), 0, 0)
        self.led_reader_spin = QSpinBox()
        self.led_reader_spin.setRange(0, 255)
        led_layout.addWidget(self.led_reader_spin, 0, 1)
        
        led_layout.addWidget(QLabel("LED Number:"), 0, 2)
        self.led_number_spin = QSpinBox()
        self.led_number_spin.setRange(0, 255)
        led_layout.addWidget(self.led_number_spin, 0, 3)
        
        led_layout.addWidget(QLabel("Control Code:"), 0, 4)
        self.led_control_spin = QSpinBox()
        self.led_control_spin.setRange(0, 255)
        self.led_control_spin.setValue(1)
        led_layout.addWidget(self.led_control_spin, 0, 5)
        
        led_layout.addWidget(QLabel("ON Time (100ms):"), 1, 0)
        self.led_on_time_spin = QSpinBox()
        self.led_on_time_spin.setRange(0, 255)
        self.led_on_time_spin.setValue(5)
        led_layout.addWidget(self.led_on_time_spin, 1, 1)
        
        led_layout.addWidget(QLabel("OFF Time (100ms):"), 1, 2)
        self.led_off_time_spin = QSpinBox()
        self.led_off_time_spin.setRange(0, 255)
        self.led_off_time_spin.setValue(5)
        led_layout.addWidget(self.led_off_time_spin, 1, 3)
        
        led_layout.addWidget(QLabel("ON Color:"), 1, 4)
        self.led_on_color_combo = QComboBox()
        self.led_on_color_combo.addItems(["Black", "Red", "Green", "Amber", "Blue", "Magenta", "Cyan", "White"])
        self.led_on_color_combo.setCurrentIndex(1)  # Default to Red
        led_layout.addWidget(self.led_on_color_combo, 1, 5)
        
        led_layout.addWidget(QLabel("OFF Color:"), 2, 0)
        self.led_off_color_combo = QComboBox()
        self.led_off_color_combo.addItems(["Black", "Red", "Green", "Amber", "Blue", "Magenta", "Cyan", "White"])
        self.led_off_color_combo.setCurrentIndex(0)  # Default to Black
        led_layout.addWidget(self.led_off_color_combo, 2, 1)
        
        led_layout.addWidget(QLabel("Timer (100ms):"), 2, 2)
        self.led_timer_spin = QSpinBox()
        self.led_timer_spin.setRange(0, 65535)
        self.led_timer_spin.setValue(10)
        led_layout.addWidget(self.led_timer_spin, 2, 3)
        
        self.led_btn = QPushButton("Set LED")
        self.led_btn.clicked.connect(self.control_led)
        led_layout.addWidget(self.led_btn, 0, 6)
        
        # Buzzer Control
        buzzer_group = QGroupBox("Buzzer Control")
        layout.addWidget(buzzer_group)
        
        buzzer_layout = QGridLayout(buzzer_group)
        
        buzzer_layout.addWidget(QLabel("Reader:"), 0, 0)
        self.buzzer_reader_spin = QSpinBox()
        self.buzzer_reader_spin.setRange(0, 255)
        buzzer_layout.addWidget(self.buzzer_reader_spin, 0, 1)
        
        buzzer_layout.addWidget(QLabel("Tone Code:"), 0, 2)
        self.buzzer_tone_spin = QSpinBox()
        self.buzzer_tone_spin.setRange(0, 255)
        self.buzzer_tone_spin.setValue(2)
        buzzer_layout.addWidget(self.buzzer_tone_spin, 0, 3)
        
        buzzer_layout.addWidget(QLabel("On Time (100ms):"), 0, 4)
        self.buzzer_on_spin = QSpinBox()
        self.buzzer_on_spin.setRange(0, 255)
        self.buzzer_on_spin.setValue(5)
        buzzer_layout.addWidget(self.buzzer_on_spin, 0, 5)
        
        buzzer_layout.addWidget(QLabel("Off Time (100ms):"), 1, 0)
        self.buzzer_off_spin = QSpinBox()
        self.buzzer_off_spin.setRange(0, 255)
        self.buzzer_off_spin.setValue(5)
        buzzer_layout.addWidget(self.buzzer_off_spin, 1, 1)
        
        buzzer_layout.addWidget(QLabel("Repeat Count:"), 1, 2)
        self.buzzer_repeat_spin = QSpinBox()
        self.buzzer_repeat_spin.setRange(0, 255)
        self.buzzer_repeat_spin.setValue(1)
        buzzer_layout.addWidget(self.buzzer_repeat_spin, 1, 3)
        
        self.buzzer_btn = QPushButton("Sound Buzzer")
        self.buzzer_btn.clicked.connect(self.control_buzzer)
        buzzer_layout.addWidget(self.buzzer_btn, 0, 6)
        
        # Output Control
        output_group = QGroupBox("Output Control")
        layout.addWidget(output_group)
        
        output_layout = QGridLayout(output_group)
        
        output_layout.addWidget(QLabel("Output Number:"), 0, 0)
        self.output_number_spin = QSpinBox()
        self.output_number_spin.setRange(0, 255)
        output_layout.addWidget(self.output_number_spin, 0, 1)
        
        output_layout.addWidget(QLabel("Control Code:"), 0, 2)
        self.output_control_spin = QSpinBox()
        self.output_control_spin.setRange(0, 255)
        self.output_control_spin.setValue(1)
        output_layout.addWidget(self.output_control_spin, 0, 3)
        
        output_layout.addWidget(QLabel("Timer (100ms):"), 0, 4)
        self.output_timer_spin = QSpinBox()
        self.output_timer_spin.setRange(0, 65535)
        self.output_timer_spin.setValue(10)
        output_layout.addWidget(self.output_timer_spin, 0, 5)
        
        self.output_btn = QPushButton("Set Output")
        self.output_btn.clicked.connect(self.control_output)
        output_layout.addWidget(self.output_btn, 0, 6)
        
        layout.addStretch()
    
    def setup_status_tab(self):
        status_widget = QWidget()
        self.tab_widget.addTab(status_widget, "Status Monitoring")
        
        layout = QVBoxLayout(status_widget)
        
        # Control buttons
        button_layout = QHBoxLayout()
        
        self.monitor_btn = QPushButton("Start Monitoring")
        self.monitor_btn.clicked.connect(self.toggle_monitoring)
        button_layout.addWidget(self.monitor_btn)
        
        self.device_info_btn = QPushButton("Get Device Info")
        self.device_info_btn.clicked.connect(self.get_device_info)
        button_layout.addWidget(self.device_info_btn)
        
        self.capabilities_btn = QPushButton("Get Capabilities")
        self.capabilities_btn.clicked.connect(self.get_capabilities)
        button_layout.addWidget(self.capabilities_btn)
        
        self.clear_log_btn = QPushButton("Clear Log")
        self.clear_log_btn.clicked.connect(self.clear_status_log)
        button_layout.addWidget(self.clear_log_btn)
        
        button_layout.addStretch()
        layout.addLayout(button_layout)
        
        # Status display
        self.status_text = QTextEdit()
        self.status_text.setReadOnly(True)
        layout.addWidget(self.status_text)
    
    def setup_firmware_tab(self):
        firmware_widget = QWidget()
        self.tab_widget.addTab(firmware_widget, "Firmware")
        
        layout = QVBoxLayout(firmware_widget)
        
        # File selection
        file_group = QGroupBox("Firmware File")
        layout.addWidget(file_group)
        
        file_layout = QHBoxLayout(file_group)
        
        self.firmware_path_edit = QLineEdit()
        file_layout.addWidget(self.firmware_path_edit)
        
        self.browse_btn = QPushButton("Browse")
        self.browse_btn.clicked.connect(self.browse_firmware)
        file_layout.addWidget(self.browse_btn)
        
        # Upload settings
        upload_group = QGroupBox("Upload Settings")
        layout.addWidget(upload_group)
        
        upload_layout = QFormLayout(upload_group)
        
        self.upload_baud_edit = QLineEdit("115200")
        upload_layout.addRow("Upload Baud Rate:", self.upload_baud_edit)
        
        self.upload_btn = QPushButton("Upload Firmware")
        self.upload_btn.clicked.connect(self.upload_firmware)
        upload_layout.addRow(self.upload_btn)
        
        # Progress
        self.progress_label = QLabel("Ready")
        upload_layout.addRow("Status:", self.progress_label)
        
        self.progress_bar = QProgressBar()
        upload_layout.addRow("Progress:", self.progress_bar)
        
        layout.addStretch()
    
    def setup_advanced_tab(self):
        advanced_widget = QWidget()
        self.tab_widget.addTab(advanced_widget, "Advanced")
        
        layout = QVBoxLayout(advanced_widget)
        
        # MFG Commands
        mfg_group = QGroupBox("Manufacturer Commands")
        layout.addWidget(mfg_group)
        
        mfg_layout = QFormLayout(mfg_group)
        
        self.mfg_cmd_combo = QComboBox()
        self.mfg_cmd_combo.addItems([cmd.name for cmd in MFG_COMMANDS])
        mfg_layout.addRow("Command:", self.mfg_cmd_combo)
        
        self.mfg_btn = QPushButton("Send MFG Command")
        self.mfg_btn.clicked.connect(self.send_mfg_command)
        mfg_layout.addRow(self.mfg_btn)
        
        # Command log
        log_group = QGroupBox("Command Log")
        layout.addWidget(log_group)
        
        log_layout = QVBoxLayout(log_group)
        
        self.command_log = QTextEdit()
        self.command_log.setReadOnly(True)
        log_layout.addWidget(self.command_log)
        
        layout.addStretch()
    
    # Connection Methods
    def connect_transport(self):
        try:
            transport_type = self.transport_combo.currentText()
            
            if transport_type == "Serial":
                # Get the actual port device path from combo box data
                current_index = self.port_combo.currentIndex()
                if current_index >= 0:
                    port = self.port_combo.itemData(current_index)
                    if not port:  # If no data, use the text (for manually entered ports)
                        port = self.port_combo.currentText().split(" - ")[0]  # Take only the port part
                else:
                    port = self.port_combo.currentText().split(" - ")[0]  # Take only the port part
                
                try:
                    baud = int(self.baud_combo.currentText())
                except ValueError:
                    QMessageBox.warning(self, "Invalid Baud Rate", "Please enter a valid baud rate")
                    return
                
                transport = SerialTransport(port, baud)
                self.log_message(f"Connecting to serial port: {port} at {baud} baud")
            else:  # USB
                transport = USBTransport()
                self.log_message("Connecting to USB transport")
            
            self.control_panel = ControlPanel(transport)
            
            self.connect_btn.setEnabled(False)
            self.disconnect_btn.setEnabled(True)
            self.discover_btn.setEnabled(True)
            
            self.log_message("Connected to transport successfully")
            
        except Exception as e:
            QMessageBox.critical(self, "Connection Error", f"Failed to connect: {str(e)}")
    
    def disconnect_transport(self):
        try:
            if self.monitor_thread and self.monitor_thread.isRunning():
                self.stop_monitoring()
            
            if self.control_panel:
                del self.control_panel
                self.control_panel = None
            
            self.current_device = None
            self.device_tree.clear()
            
            self.connect_btn.setEnabled(True)
            self.disconnect_btn.setEnabled(False)
            self.discover_btn.setEnabled(False)
            self.secure_btn.setEnabled(False)
            self.reset_secure_btn.setEnabled(False)
            
            self.log_message("Disconnected from transport")
            
        except Exception as e:
            QMessageBox.critical(self, "Disconnect Error", f"Failed to disconnect: {str(e)}")
    
    def discover_devices(self):
        if not self.control_panel:
            return
        
        self.discover_btn.setEnabled(False)
        self.device_tree.clear()
        
        # Create worker thread for discovery
        self.discovery_worker = DeviceDiscoveryWorker(self.control_panel, 5)
        self.discovery_thread = QThread()
        
        self.discovery_worker.moveToThread(self.discovery_thread)
        self.discovery_worker.device_found.connect(self.on_device_discovered)
        self.discovery_worker.discovery_finished.connect(self.on_discovery_finished)
        self.discovery_worker.error_occurred.connect(self.on_discovery_error)
        
        self.discovery_thread.started.connect(self.discovery_worker.discover)
        self.discovery_thread.start()
        
        self.log_message("Starting device discovery...")
    
    def on_device_discovered(self, address: int, serial_number: int, status: str):
        item = QTreeWidgetItem([str(address), str(serial_number), status])
        self.device_tree.addTopLevelItem(item)
        self.log_message(f"Device discovered - Address: {address}, Serial: {serial_number}")
    
    def on_discovery_finished(self):
        self.discovery_thread.quit()
        self.discovery_thread.wait()
        self.discover_btn.setEnabled(True)
        self.log_message("Device discovery completed")
    
    def on_discovery_error(self, error: str):
        self.discovery_thread.quit()
        self.discovery_thread.wait()
        self.discover_btn.setEnabled(True)
        QMessageBox.critical(self, "Discovery Error", f"Discovery failed: {error}")
    
    def on_device_selected(self):
        selected_items = self.device_tree.selectedItems()
        if not selected_items or not self.control_panel:
            return
        
        item = selected_items[0]
        address = int(item.text(0))
        
        # Find device by address
        for device in self.control_panel.devices:
            if device.pd_address == address:
                self.current_device = device
                self.secure_btn.setEnabled(True)
                self.reset_secure_btn.setEnabled(True)
                
                # Show current secure channel status
                if device.sc and device.sc.secure_channel_active:
                    self.log_message(f"Selected device at address {address} (secure channel active)")
                else:
                    self.log_message(f"Selected device at address {address} (unencrypted mode)")
                break
    
    def on_device_double_clicked(self, item: QTreeWidgetItem, column: int):
        """Handle double-click on device to set address"""
        if not self.control_panel:
            return
        
        current_address = int(item.text(0))
        serial_number = int(item.text(1))
        
        # Find the device object
        target_device = None
        for device in self.control_panel.devices:
            if device.pd_address == current_address and device.serial_number == serial_number:
                target_device = device
                break
        
        if not target_device:
            QMessageBox.warning(self, "Warning", "Device not found")
            return
        
        # Show address setting dialog
        dialog = SetAddressDialog(current_address, serial_number, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_address = dialog.get_new_address()
            
            if new_address != current_address:
                self.set_device_address(target_device, new_address, item)
    
    def set_device_address(self, device: OsdpDevice, new_address: int, tree_item: QTreeWidgetItem):
        """Set new address for a device using MFG command"""
        try:
            # Check if new address is already in use
            for existing_device in self.control_panel.devices:
                if existing_device.pd_address == new_address and existing_device != device:
                    QMessageBox.warning(self, "Warning", f"Address {new_address} is already in use")
                    return
            
            # Send MFG command to set new address
            from TKH_OSDP.commands.osdp_MFG import MFG_COMMANDS, osdp_MFGCommand
            
            mfg_cmd = osdp_MFGCommand(
                MFG_COMMANDS.OSDP_MFG_CMD_SET_ADDRESS, 
                [
                    (device.serial_number) & 0xFF, 
                    (device.serial_number >> 8) & 0xFF, 
                    (device.serial_number >> 16) & 0xFF, 
                    (device.serial_number >> 24) & 0xFF, 
                    new_address
                ]
            )
            
            result = device.MFG(mfg_cmd)
            match result:
                case Ok(_):
                    # Update device address
                    old_address = device.pd_address
                    device.pd_address = new_address
                    device.sequence = 0  # Reset sequence after address change
                    
                    # Update tree item
                    tree_item.setText(0, str(new_address))
                    
                    self.log_message(f"Device address changed from {old_address} to {new_address}")
                    QMessageBox.information(self, "Success", f"Device address set to {new_address}")
                    
                    # If this was the currently selected device, update selection
                    if self.current_device == device:
                        self.log_message(f"Updated selected device address to {new_address}")
                        
                case Err(error):
                    self.log_message(f"Failed to set device address: {error}")
                    QMessageBox.critical(self, "Error", f"Failed to set device address: {error}")
                    
        except Exception as e:
            QMessageBox.critical(self, "Address Setting Error", f"Failed to set device address: {str(e)}")
    
    def setup_secure_channel(self):
        if not self.current_device:
            return
        
        try:
            # Stop monitoring if it's running to reset the channel cleanly
            was_monitoring = self.monitor_thread and self.monitor_thread.isRunning()
            if was_monitoring:
                self.stop_monitoring()
                self.log_message("Stopped monitoring to establish secure channel")
            
            key = self.key_edit.text()
            self.current_device.setupSecureChannel(key)
            self.log_message("Secure channel established successfully")
            
            # Restart monitoring if it was running before
            if was_monitoring:
                self.start_monitoring()
                self.log_message("Restarted monitoring with secure channel")
                QMessageBox.information(self, "Success", "Secure channel established and monitoring restarted")
            else:
                # Offer to start monitoring
                reply = QMessageBox.question(
                    self, 
                    "Start Monitoring", 
                    "Secure channel established. Would you like to start monitoring?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                
                if reply == QMessageBox.StandardButton.Yes:
                    self.start_monitoring()
                    QMessageBox.information(self, "Success", "Secure channel established and monitoring started")
                else:
                    QMessageBox.information(self, "Success", "Secure channel established")
                    
        except Exception as e:
            QMessageBox.critical(self, "Security Error", f"Failed to setup secure channel: {str(e)}")
    
    # Control Methods
    def control_led(self):
        if not self.current_device:
            QMessageBox.warning(self, "Warning", "No device selected")
            return
        
        try:
            cmd = osdp_LEDCommand(
                reader_number=self.led_reader_spin.value(),
                LED_number=self.led_number_spin.value(),
                control_code=self.led_control_spin.value(),
                ON_time=self.led_on_time_spin.value(),
                Off_time=self.led_off_time_spin.value(),
                ON_color=self.led_on_color_combo.currentIndex(),
                OFF_color=self.led_off_color_combo.currentIndex(),
                timer=self.led_timer_spin.value(),
                control_code_perm=0, ON_time_perm=0,
                Off_time_perm=0, ON_color_perm=0, OFF_color_perm=0
            )
            
            result = self.current_device.LED(cmd)
            match result:
                case Ok(response):
                    color_names = ["Black", "Red", "Green", "Amber", "Blue", "Magenta", "Cyan", "White"]
                    on_color_name = color_names[self.led_on_color_combo.currentIndex()]
                    off_color_name = color_names[self.led_off_color_combo.currentIndex()]
                    self.log_message(f"LED command sent successfully - Reader: {self.led_reader_spin.value()}, LED: {self.led_number_spin.value()}, Control: {self.led_control_spin.value()}, ON: {self.led_on_time_spin.value()}x100ms ({on_color_name}), OFF: {self.led_off_time_spin.value()}x100ms ({off_color_name}), Timer: {self.led_timer_spin.value()}x100ms")
                case Err(error):
                    self.log_message(f"LED command failed: {error}")
                    
        except Exception as e:
            QMessageBox.critical(self, "LED Error", f"Failed to control LED: {str(e)}")
    
    def control_buzzer(self):
        if not self.current_device:
            QMessageBox.warning(self, "Warning", "No device selected")
            return
        
        try:
            cmd = osdp_BuzzerCommand(
                reader_number=self.buzzer_reader_spin.value(),
                tone_code=self.buzzer_tone_spin.value(),
                on_time=self.buzzer_on_spin.value(),
                off_time=self.buzzer_off_spin.value(),
                rep_count=self.buzzer_repeat_spin.value()
            )
            
            result = self.current_device.Buzzer(cmd)
            match result:
                case Ok(response):
                    self.log_message(f"Buzzer command sent successfully - Reader: {self.buzzer_reader_spin.value()}, Tone: {self.buzzer_tone_spin.value()}, On: {self.buzzer_on_spin.value()}x100ms, Off: {self.buzzer_off_spin.value()}x100ms, Repeats: {self.buzzer_repeat_spin.value()}")
                case Err(error):
                    self.log_message(f"Buzzer command failed: {error}")
                    
        except Exception as e:
            QMessageBox.critical(self, "Buzzer Error", f"Failed to control buzzer: {str(e)}")
    
    def control_output(self):
        if not self.current_device:
            QMessageBox.warning(self, "Warning", "No device selected")
            return
        
        try:
            cmd = osdp_OutputCommand(
                output_no=self.output_number_spin.value(),
                control_code=self.output_control_spin.value(),
                timer=self.output_timer_spin.value()
            )
            
            result = self.current_device.Output(cmd)
            match result:
                case Ok(response):
                    control_desc = "ON" if self.output_control_spin.value() == 1 else "OFF" if self.output_control_spin.value() == 0 else f"Code {self.output_control_spin.value()}"
                    self.log_message(f"Output command sent successfully - Output: {self.output_number_spin.value()}, Control: {control_desc}, Timer: {self.output_timer_spin.value()}x100ms")
                case Err(error):
                    self.log_message(f"Output command failed: {error}")
                    
        except Exception as e:
            QMessageBox.critical(self, "Output Error", f"Failed to control output: {str(e)}")
    
    # Status Methods
    def toggle_monitoring(self):
        if not self.current_device:
            QMessageBox.warning(self, "Warning", "No device selected")
            return
        
        if self.monitor_thread and self.monitor_thread.isRunning():
            self.stop_monitoring()
        else:
            self.start_monitoring()
    
    def start_monitoring(self):
        if not self.current_device:
            QMessageBox.warning(self, "Warning", "No device selected")
            return
        
        # Check if secure channel is active, if not, offer to establish it
        if not (self.current_device.sc and self.current_device.sc.secure_channel_active):
            reply = QMessageBox.question(
                self, 
                "Secure Channel", 
                "Secure channel is not active. Would you like to establish it before monitoring?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            
            if reply == QMessageBox.StandardButton.Yes:
                try:
                    key = self.key_edit.text()
                    self.current_device.setupSecureChannel(key)
                    self.log_message("Secure channel established before starting monitoring")
                except Exception as e:
                    QMessageBox.critical(self, "Security Error", f"Failed to setup secure channel: {str(e)}")
                    return
        
        self.monitor_worker = StatusMonitorWorker(self.current_device)
        self.monitor_thread = QThread()
        
        self.monitor_worker.moveToThread(self.monitor_thread)
        self.monitor_worker.status_update.connect(self.log_message)
        
        self.monitor_thread.started.connect(self.monitor_worker.monitor)
        self.monitor_thread.start()
        
        self.monitor_btn.setText("Stop Monitoring")
        secure_status = "with secure channel" if (self.current_device.sc and self.current_device.sc.secure_channel_active) else "in unencrypted mode"
        self.log_message(f"Started status monitoring {secure_status}")
    
    def stop_monitoring(self):
        if self.monitor_worker:
            self.monitor_worker.stop()
        
        if self.monitor_thread:
            self.monitor_thread.quit()
            self.monitor_thread.wait()
        
        # Reset secure channel when stopping monitoring
        if self.current_device and self.current_device.sc and self.current_device.sc.secure_channel_active:
            self.current_device.resetChannel()
            self.log_message("Secure channel reset - device back to unencrypted mode")
        
        self.monitor_btn.setText("Start Monitoring")
        self.log_message("Stopped status monitoring")
    
    def get_device_info(self):
        if not self.current_device:
            QMessageBox.warning(self, "Warning", "No device selected")
            return
        
        try:
            cmd = osdp_IdReportCommand()
            result = self.current_device.IdReport(cmd)
            
            match result:
                case Ok(response):
                    info = f"""Device Information:
Vendor Code: {response.vendor_code}
Model Number: {response.model_number}
Version: {response.version}
Serial Number: {response.serial_number}"""
                    self.log_message(info)
                case Err(error):
                    self.log_message(f"Failed to get device info: {error}")
                    
        except Exception as e:
            QMessageBox.critical(self, "Device Info Error", f"Failed to get device info: {str(e)}")
    
    def get_capabilities(self):
        if not self.current_device:
            QMessageBox.warning(self, "Warning", "No device selected")
            return
        
        try:
            cmd = osdp_CAPCommand()
            result = self.current_device.CAP(cmd)
            
            match result:
                case Ok(response):
                    caps = "Device Capabilities:\n"
                    for record in response.records:
                        caps += f"- {record.code.name}: {record.number} (compatibility: {record.compatibility})\n"
                    self.log_message(caps)
                case Err(error):
                    self.log_message(f"Failed to get capabilities: {error}")
                    
        except Exception as e:
            QMessageBox.critical(self, "Capabilities Error", f"Failed to get capabilities: {str(e)}")
    
    def clear_status_log(self):
        self.status_text.clear()
    
    # Firmware Methods
    def browse_firmware(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Firmware File", "", "Binary Files (*.bin);;All Files (*)"
        )
        if file_path:
            self.firmware_path_edit.setText(file_path)
    
    def upload_firmware(self):
        if not self.current_device:
            QMessageBox.warning(self, "Warning", "No device selected")
            return
        
        firmware_path = self.firmware_path_edit.text()
        if not firmware_path or not Path(firmware_path).exists():
            QMessageBox.warning(self, "Warning", "Please select a valid firmware file")
            return
        
        try:
            baud_rate = int(self.upload_baud_edit.text())
            
            self.upload_btn.setEnabled(False)
            self.progress_bar.setRange(0, 0)  # Indeterminate progress
            self.progress_label.setText("Uploading firmware...")
            
            # Run firmware upload in thread to prevent UI blocking
            def upload_worker():
                try:
                    result = self.current_device.upload_firmware_osdp(firmware_path, baud_rate)
                    match result:
                        case Ok(_):
                            self.progress_label.setText("Firmware uploaded successfully")
                            self.log_message("Firmware upload completed successfully")
                        case Err(error):
                            self.progress_label.setText(f"Upload failed: {error}")
                            self.log_message(f"Firmware upload failed: {error}")
                except Exception as e:
                    self.progress_label.setText(f"Upload error: {str(e)}")
                    self.log_message(f"Firmware upload error: {str(e)}")
                finally:
                    self.upload_btn.setEnabled(True)
                    self.progress_bar.setRange(0, 100)
                    self.progress_bar.setValue(100)
            
            thread = threading.Thread(target=upload_worker)
            thread.daemon = True
            thread.start()
            
        except ValueError:
            QMessageBox.warning(self, "Warning", "Invalid baud rate")
        except Exception as e:
            QMessageBox.critical(self, "Upload Error", f"Failed to start upload: {str(e)}")
    
    # Advanced Methods
    def send_mfg_command(self):
        if not self.current_device:
            QMessageBox.warning(self, "Warning", "No device selected")
            return
        
        try:
            cmd_name = self.mfg_cmd_combo.currentText()
            cmd_enum = MFG_COMMANDS[cmd_name]
            
            cmd = osdp_MFGCommand(cmd_enum, [])
            result = self.current_device.MFG(cmd)
            
            match result:
                case Ok(response):
                    self.log_message(f"MFG command {cmd_name} sent successfully")
                    self.command_log.append(f"MFG Response: {response.data}")
                case Err(error):
                    self.log_message(f"MFG command {cmd_name} failed: {error}")
                    
        except Exception as e:
            QMessageBox.critical(self, "MFG Command Error", f"Failed to send MFG command: {str(e)}")
    
    def reset_secure_channel(self):
        """Reset device to unencrypted mode"""
        if not self.current_device:
            return
        
        try:
            # Stop monitoring if it's running
            was_monitoring = self.monitor_thread and self.monitor_thread.isRunning()
            if was_monitoring:
                self.stop_monitoring()
                self.log_message("Stopped monitoring to reset secure channel")
            
            # Reset the secure channel
            self.current_device.resetChannel()
            self.log_message("Device reset to unencrypted mode")
            
            # Restart monitoring if it was running before
            if was_monitoring:
                self.start_monitoring()
                self.log_message("Restarted monitoring in unencrypted mode")
                QMessageBox.information(self, "Success", "Device reset to unencrypted mode and monitoring restarted")
            else:
                QMessageBox.information(self, "Success", "Device reset to unencrypted mode")
                
        except Exception as e:
            QMessageBox.critical(self, "Reset Error", f"Failed to reset secure channel: {str(e)}")
    
    # Utility Methods
    def log_message(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}"
        
        # Only append to status_text if it exists
        if hasattr(self, 'status_text'):
            self.status_text.append(formatted_message)
        
        # Also log to command log if it exists and it's a command-related message
        if hasattr(self, 'command_log') and any(word in message.lower() for word in ['command', 'response', 'mfg']):
            self.command_log.append(formatted_message)
    
    def closeEvent(self, event):
        """Handle application close"""
        if self.monitor_thread and self.monitor_thread.isRunning():
            self.stop_monitoring()
        
        if self.discovery_thread and self.discovery_thread.isRunning():
            if self.discovery_worker:
                self.discovery_worker.stop()
            self.discovery_thread.quit()
            self.discovery_thread.wait()
        
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("OSDP Control Panel")
    app.setApplicationVersion("1.0")
    
    window = OSDPMainWindow()
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
