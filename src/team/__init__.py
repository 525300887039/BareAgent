"""Team collaboration modules for BareAgent."""

from src.team.autonomous import AutonomousAgent
from src.team.mailbox import Message, MessageBus
from src.team.manager import AgentInstance, Teammate, TeammateManager
from src.team.protocols import Protocol, ProtocolFSM

__all__ = [
    "AgentInstance",
    "AutonomousAgent",
    "Message",
    "MessageBus",
    "Protocol",
    "ProtocolFSM",
    "Teammate",
    "TeammateManager",
]
