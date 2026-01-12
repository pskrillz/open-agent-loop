"""
Loop control logic for agent iterations.
Pure functions for detecting completion, repetition, and determining when to stop.
"""

import json
import hashlib
from typing import Tuple, List, Optional, Dict, Any

# Completion signal phrases (lowercase for case-insensitive matching)
COMPLETION_PHRASES = [
    "task complete",
    "task is complete",
    "i'm done",
    "i am done",
    "finished",
    "successfully completed",
    "all done",
    "that's all",
    "that completes",
]


def create_tool_signature(tool_name: str, tool_input: Dict[str, Any]) -> Tuple[str, str]:
    """
    Create a signature tuple for a tool call to track uniqueness.
    
    Args:
        tool_name: Name of the tool being called
        tool_input: Dictionary of arguments passed to the tool
    
    Returns:
        (tool_name, arg_hash) tuple where arg_hash is a short hash of the arguments
    
    This allows detecting when the SAME tool is called with the SAME arguments repeatedly,
    which is true repetitive behavior, vs calling the same tool with different arguments,
    which is legitimate investigation/work.
    """
    # Sort keys for consistent hashing
    normalized = json.dumps(tool_input, sort_keys=True, default=str)
    # Create short hash (first 12 chars sufficient for collision avoidance)
    arg_hash = hashlib.sha256(normalized.encode()).hexdigest()[:12]
    return (tool_name, arg_hash)


def detect_completion_signals(response: str, tool_calls: list) -> Tuple[bool, str]:
    """
    Detect if agent is signaling task completion.
    
    Args:
        response: The LLM's text response
        tool_calls: List of tool calls from the LLM
    
    Returns:
        (is_complete, reason) tuple
        
    Detection criteria:
    1. No tool calls AND response contains completion phrases
    2. Response is short (<100 chars) with no tool calls (likely acknowledgment)
    """
    # If agent is still calling tools, it's not complete
    if tool_calls:
        return False, ""
    
    response_lower = response.lower().strip()
    
    # Check for explicit completion phrases
    for phrase in COMPLETION_PHRASES:
        if phrase in response_lower:
            return True, f"Agent signaled completion with phrase: '{phrase}'"
    
    # Very short response with no tool calls might indicate completion
    if len(response) < 100 and len(response) > 0:
        # But avoid false positives on questions or requests for clarification
        question_indicators = ["?", "should i", "would you like", "do you want", "shall i"]
        if not any(indicator in response_lower for indicator in question_indicators):
            return True, "Agent provided brief response with no further actions"
    
    return False, ""


def detect_repetitive_behavior(
    tool_history: List[Tuple[str, str]], 
    window_size: int = 3
) -> Tuple[bool, str]:
    """
    Detect if agent is repeating same tool pattern in a loop.
    
    Args:
        tool_history: List of (tool_name, arg_hash) tuples (oldest first)
        window_size: Number of consecutive calls to check for repetition
    
    Returns:
        (is_repeating, reason) tuple
        
    Now checks BOTH tool name AND arguments. Only flags as repetitive when
    the same tool is called with identical arguments multiple times.
    
    Detects patterns like:
    - Same tool with same args 5+ times: [(bash, abc123), (bash, abc123), ...]
    - Alternating identical calls: [(bash, abc), (grep, def), (bash, abc), (grep, def), ...]
    - Repeated sequences: [(a, x), (b, y), (c, z), (a, x), (b, y), (c, z)]
    """
    # Need at least some history to detect patterns
    if len(tool_history) < 5:
        return False, ""
    
    # Check for identical tool calls (same tool + args) 5+ times in a row
    if len(tool_history) >= 5:
        last_five = tool_history[-5:]
        if len(set(last_five)) == 1:
            tool_name = last_five[0][0]
            return True, f"Identical call to '{tool_name}' (same arguments) repeated 5 times"
    
    # Check for alternating pattern (A, B, A, B, A, B) with same arguments
    if len(tool_history) >= 6:
        last_six = tool_history[-6:]
        # Check if it's an alternating pattern
        if (last_six[0] == last_six[2] == last_six[4] and 
            last_six[1] == last_six[3] == last_six[5] and 
            last_six[0] != last_six[1]):
            tool_a = last_six[0][0]
            tool_b = last_six[1][0]
            pattern = f"{tool_a} ↔ {tool_b}"
            return True, f"Detected alternating pattern with identical args: {pattern}"
    
    # Check for repeated sequence patterns (ABC, ABC) with same arguments
    if len(tool_history) >= window_size * 2:
        recent_tools = tool_history[-window_size * 2:]
        first_half = recent_tools[:window_size]
        second_half = recent_tools[window_size:]
        
        if first_half == second_half:
            tool_names = [t[0] for t in first_half]
            pattern = " → ".join(tool_names)
            return True, f"Detected repetitive sequence with identical args: {pattern}"
    
    return False, ""


def should_continue_iteration(
    iteration: int,
    max_iterations: int,
    response: str,
    tool_calls: list,
    tool_history: List[Tuple[str, str]],
) -> Tuple[bool, str, Optional[str]]:
    """
    Main decision function - composes all checks to determine if agent should continue.
    
    Args:
        iteration: Current iteration number (1-indexed)
        max_iterations: Maximum allowed iterations
        response: LLM's text response
        tool_calls: List of tool calls from LLM
        tool_history: History of all tool names called
    
    Returns:
        (should_stop, stop_reason, user_prompt_message) tuple
        - should_stop: True if agent should stop iterating
        - stop_reason: Human-readable reason for stopping
        - user_prompt_message: If not None, prompt user before stopping (soft stop)
                               If None, stop immediately (hard stop)
    """
    # Hard stop: Max iterations reached
    if iteration >= max_iterations:
        return True, f"Maximum iteration limit reached ({max_iterations})", None
    
    # Hard stop: Repetitive behavior detected
    is_repeating, repeat_reason = detect_repetitive_behavior(tool_history)
    if is_repeating:
        return True, f"Repetitive behavior detected: {repeat_reason}", None
    
    # Soft stop: Completion signals detected
    is_complete, completion_reason = detect_completion_signals(response, tool_calls)
    if is_complete:
        user_message = (
            f"\n🎯 Completion detected (iteration {iteration}/{max_iterations}): {completion_reason}\n"
            f"Do you want the agent to continue? [y/N]: "
        )
        return True, completion_reason, user_message
    
    # Continue iterating
    return False, "", None
