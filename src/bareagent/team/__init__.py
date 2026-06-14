"""Team collaboration modules for BareAgent."""

from bareagent.team.autonomous import AutonomousAgent
from bareagent.team.mailbox import Message, MessageBus
from bareagent.team.manager import AgentInstance, Teammate, TeammateManager
from bareagent.team.protocols import Protocol, ProtocolFSM

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
