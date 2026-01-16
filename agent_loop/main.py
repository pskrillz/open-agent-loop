#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["anthropic>=0.45.0", "openai>=1.0.0"]
# ///
import os
import json
from typing import Dict, List, Optional, Any
from agent_loop.providers.anthropic import create_anthropic_llm
from agent_loop.providers.openai import create_openai_llm
from agent_loop.tools import TOOLS, TOOL_HANDLERS, display_custom_tools
import argparse
from halo import Halo
from dotenv import load_dotenv
import asyncio
from contextlib import AsyncExitStack, suppress
from agent_loop.mcp_client import MCPManager
import inspect
import datetime
from agent_loop.output import (
    agent_reply,
    agent_tool,
    agent_confirm,
    agent_error,
    agent_info,
)
from agent_loop.cli_input import get_user_command, ask_continue
from agent_loop.signals import setup_signal_handlers
from agent_loop.async_utils import execute_with_interrupt
from agent_loop.constants import (
    PLAIN_FORMAT_INSTRUCTION,
    MARKDOWN_FORMAT_INSTRUCTION,
    HELP_MESSAGE,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_PROMPT_ON_COMPLETION,
)
from agent_loop.exceptions import GracefulExit
from agent_loop.loop_control import should_continue_iteration, create_tool_signature
import importlib.metadata

# Load environment variables - local .env takes priority over config directory
load_dotenv(
    dotenv_path=os.path.expanduser("~/.config/agent-loop/.env"), override=True
)  # Config defaults first
load_dotenv(dotenv_path=".env", override=False)  # Local .env overrides config

mcp_manager = MCPManager()


def display_welcome_message():
    """
    Display a beautiful welcome message with the current version.
    """
    version = importlib.metadata.version("agent-loop")
    welcome_message = f"""
╭────────────────────────────────────────────────────────╮
│                                                        │
│   █████╗  ██████╗ ███████╗███╗   ██╗████████╗          │
│  ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝          │
│  ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║             │
│  ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║             │
│  ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║             │
│  ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝             │
│                                                        │
│  ██╗      ██████╗  ██████╗ ██████╗                     │
│  ██║     ██╔═══██╗██╔═══██╗██╔══██╗                    │
│  ██║     ██║   ██║██║   ██║██████╔╝                    │
│  ██║     ██║   ██║██║   ██║██╔═══╝                     │
│  ███████╗╚██████╔╝╚██████╔╝██║                         │
│  ╚══════╝ ╚═════╝  ╚═════╝ ╚═╝                         │
│                                                        │
│  Version {version:<43}   │
╰────────────────────────────────────────────────────────╯
    """
    print(welcome_message)


async def run_llm(llm_fn, msg):
    """
    Call llm_fn with msg, supporting both sync and async LLM functions.
    """
    if inspect.iscoroutinefunction(llm_fn):
        return await llm_fn(msg)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, llm_fn, msg)


class AgentLoop:
    """
    Main event loop and state manager for the agent-loop CLI application.
    Handles user input, tool calls, LLM interaction, and signal/key interruption.
    """

    def __init__(
        self,
        debug: bool = False,
        safe: bool = False,
        simple_text: bool = False,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        prompt_on_completion: bool = DEFAULT_PROMPT_ON_COMPLETION,
    ):
        """
        Initialize the AgentLoop.
        :param debug: Show tool input/output for debugging.
        :param safe: Require confirmation before executing tools.
        :param simple_text: Use plain text output instead of markdown.
        :param max_iterations: Maximum number of agent iteration cycles.
        :param prompt_on_completion: Prompt user when completion is detected.
        """
        self.debug = debug
        self.safe = safe
        self.simple_text = simple_text
        self.max_iterations = max_iterations
        self.prompt_on_completion = prompt_on_completion
        self.current_iteration = 0
        self.tool_call_history: list[tuple[str, str]] = []
        self.interrupt_event: asyncio.Event = asyncio.Event()

    def user_input(self) -> Optional[List[Dict]]:
        """
        Prompt the user for input using get_user_command, supporting CTRL+D or 'exit'/'quit' for quit and CTRL+C for prompt interruption.
        Returns a message list suitable for LLM input, or None if the user wants to quit.
        """
        user_input = get_user_command(self.simple_text)
        if user_input is None:
            return None

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        format_instruction = (
            PLAIN_FORMAT_INSTRUCTION
            if self.simple_text
            else MARKDOWN_FORMAT_INSTRUCTION
        )
        message_text = (
            f"{user_input}\n(Current date and time: {now})\n{format_instruction}"
        )
        return [{"type": "text", "text": message_text}]

    def _get_tool_info(self, tool_name: str) -> tuple[str, str, bool]:
        """Get tool type, icon, and MCP status for a tool name."""
        is_mcp_tool = "-" in tool_name
        tool_icon = "🔌" if is_mcp_tool else "🛠️"
        tool_type = "MCP" if is_mcp_tool else "Tool"
        return tool_type, tool_icon, is_mcp_tool

    def get_tool_description(self, tool_name: str) -> str:
        """
        Get the description for a tool by name.
        """
        for tool in TOOLS:
            if tool.get("name") == tool_name:
                return tool.get("description", "No description available.")
        return "No description available."

    async def execute_llm_phase(
        self, llm_fn: callable, msg: Any
    ) -> tuple[bool, Optional[tuple[str, list]]]:
        """
        Execute LLM call with interrupt and error handling.
        
        Args:
            llm_fn: The LLM function to call
            msg: Message content to send to LLM
        
        Returns:
            (was_interrupted, result_or_none) tuple
            - was_interrupted: True if interrupted or error occurred
            - result_or_none: (response, tool_calls) tuple if successful, None otherwise
        """
        spinner = Halo(
            text=f"Thinking... (iteration {self.current_iteration}/{self.max_iterations})",
            spinner="dots",
        )
        spinner.start()
        
        try:
            # Execute LLM with interrupt support
            was_interrupted, result = await execute_with_interrupt(
                run_llm(llm_fn, msg), self.interrupt_event, spinner
            )
            
            if was_interrupted:
                return True, None
            
            # Successfully completed
            spinner.stop()
            return False, result
            
        except asyncio.CancelledError:
            # Task was cancelled
            spinner.stop()
            return True, None
        except Exception as e:
            # General error handling
            spinner.stop()
            error_msg = f"❌ [LLM Error] {type(e).__name__}: {str(e)}"
            if self.debug:
                import traceback
                error_msg += f"\n\nLLM Error Stack trace:\n{traceback.format_exc()}"
            agent_error(error_msg, simple_text=self.simple_text)
            return True, None  # Treat as interrupt to get new input
        finally:
            spinner.stop()

    async def execute_tools_phase(
        self, tool_calls: list
    ) -> tuple[bool, Optional[list]]:
        """
        Execute all tool calls with interrupt and error handling.
        
        Args:
            tool_calls: List of tool call dicts from LLM
        
        Returns:
            (was_interrupted, tool_results_or_none) tuple
            - was_interrupted: True if interrupted
            - tool_results_or_none: List of tool results if successful, None if interrupted
        """
        tool_results = []
        
        # Track tool signatures (name + arguments) in history
        for tc in tool_calls:
            tool_signature = create_tool_signature(tc["name"], tc.get("input", {}))
            self.tool_call_history.append(tool_signature)
        
        # Execute each tool
        for tc in tool_calls:
            try:
                was_interrupted, result = await execute_with_interrupt(
                    self.handle_tool_call(tc), self.interrupt_event
                )
                
                if was_interrupted:
                    return True, None
                
                tool_results.append(result)
                
            except asyncio.CancelledError:
                # This is expected when a task is cancelled due to interruption
                if self.debug:
                    tool_type, _, _ = self._get_tool_info(tc["name"])
                    agent_info(
                        f"{tool_type} '{tc['name']}' was cancelled",
                        simple_text=self.simple_text,
                    )
                return True, None
            except asyncio.InvalidStateError as e:
                # Handle asyncio state errors
                tool_type, _, _ = self._get_tool_info(tc["name"])
                error_message = f"❌ [Asyncio Error] {tool_type} '{tc['name']}' encountered an invalid state: {str(e)}"
                if self.debug:
                    import traceback
                    error_message += (
                        f"\n\nAsyncio Error Details:\n{traceback.format_exc()}"
                    )
                agent_error(error_message, simple_text=self.simple_text)
                
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": [{"type": "text", "text": error_message}],
                    }
                )
            except Exception as e:
                # Handle TaskGroup and other unhandled exceptions with detailed reporting
                tool_type, _, _ = self._get_tool_info(tc["name"])
                error_type = type(e).__name__
                error_message = f"❌ [Execution Error] Failed to process {tool_type.lower()} '{tc['name']}': {error_type}: {str(e)}"
                
                # Special handling for ExceptionGroup/TaskGroup errors
                if hasattr(e, "exceptions") and hasattr(e, "__cause__"):
                    error_message += f"\n📋 Exception Group Details:"
                    if hasattr(e, "exceptions"):
                        for i, sub_exc in enumerate(e.exceptions, 1):
                            error_message += f"\n  {i}. {type(sub_exc).__name__}: {str(sub_exc)}"
                
                if self.debug:
                    import traceback
                    error_message += (
                        f"\n\n🔍 Full Stack Trace:\n{traceback.format_exc()}"
                    )
                    error_message += f"\n\n🔧 {tool_type} Input: {json.dumps(tc.get('input', {}), indent=2)}"
                    error_message += (
                        f"\n\n⚙️ {tool_type} ID: {tc.get('id', 'unknown')}"
                    )
                
                agent_error(error_message, simple_text=self.simple_text)
                
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": [{"type": "text", "text": error_message}],
                    }
                )
        
        return False, tool_results

    async def handle_loop_control_decision(
        self, response: str, tool_calls: list
    ) -> Optional[List[Dict]]:
        """
        Check if should continue iterating and handle the decision.
        
        This handles both soft stops (user prompt) and hard stops (immediate).
        Eliminates duplication of loop control logic.
        
        Args:
            response: LLM's text response
            tool_calls: List of tool calls from LLM
        
        Returns:
            - Next message for LLM if should continue
            - None if should exit
        """
        should_stop, stop_reason, user_prompt = should_continue_iteration(
            self.current_iteration,
            self.max_iterations,
            response,
            tool_calls,
            self.tool_call_history,
        )
        
        if not should_stop:
            # Continue normally - get next user input
            # Reset counter and history for new user request
            self.current_iteration = 0
            self.tool_call_history.clear()
            return self.user_input()
        
        # Handle stop decision
        if user_prompt and self.prompt_on_completion:
            # Soft stop - ask user if they want to continue
            if ask_continue(user_prompt, self.simple_text):
                agent_info(
                    "▶️  Continuing at user request...", simple_text=self.simple_text
                )
                # Reset iteration counter to give more room
                self.current_iteration = 0
                self.tool_call_history.clear()
                return self.user_input()
            else:
                # User chose to stop
                agent_info(f"🛑 Stopping: {stop_reason}", simple_text=self.simple_text)
                return None
        else:
            # Hard stop - no user prompt
            agent_error(f"🛑 Stopping: {stop_reason}", simple_text=self.simple_text)
            return None

    def confirm_tool_execution(self, tool_name: str, input_data: Dict) -> bool:
        """
        Ask the user to confirm execution of a tool, showing its description and input.
        Returns True if confirmed, False otherwise.
        """
        description = self.get_tool_description(tool_name)
        agent_confirm(
            f"\n⚠️ [CONFIRMATION REQUIRED]\nTool: {tool_name}\nDescription: {description}\nInput: {input_data}",
            simple_text=self.simple_text,
        )
        answer = input("Do you want to execute this command? [y/N]: ").strip().lower()
        return answer in {"y", "yes"}

    async def handle_tool_call(self, tool_call: Dict) -> Dict:
        """
        Execute a tool call, handling confirmation, debug output, and async/sync handlers.
        Returns a tool_result dict for the agent loop.
        """
        name = tool_call["name"]
        input_data = tool_call["input"]

        # Use different icons for regular tools vs MCP tools
        # MCP tools have format: server-name-tool-name (contains dash)
        tool_type, tool_icon, is_mcp_tool = self._get_tool_info(name)

        agent_tool(
            f"{tool_icon} [Agent] Calling {tool_type.lower()}: {name} | Input: {input_data}",
            simple_text=self.simple_text,
        )

        if self.debug:
            agent_info(
                f"\n[{tool_type}: {name}] Input: {input_data}\n",
                simple_text=self.simple_text,
            )

        if self.safe and not self.confirm_tool_execution(name, input_data):
            return {
                "type": "tool_result",
                "tool_use_id": tool_call["id"],
                "content": [
                    {
                        "type": "text",
                        "text": f"⚠️ [SKIPPED] {name} command was not executed by user request.",
                    },
                ],
            }

        handler = TOOL_HANDLERS.get(name)
        if not handler:
            agent_error(f"No handler for tool: {name}", simple_text=self.simple_text)
            raise ValueError(f"No handler for tool: {name}")

        try:
            if inspect.iscoroutinefunction(handler):
                output = await handler(input_data)
            else:
                output = handler(input_data)

            if self.debug:
                agent_info(str(output), simple_text=self.simple_text)

            return {
                "type": "tool_result",
                "tool_use_id": tool_call["id"],
                "content": [{"type": "text", "text": output}],
            }
        except Exception as e:
            error_message = (
                f"❌ [ERROR] {tool_type} '{name}' failed: {type(e).__name__}: {str(e)}"
            )

            if self.debug:
                import traceback

                error_message += f"\n\nInputs: {json.dumps(input_data)}\n\n"
                error_message += f"Stack trace:\n{traceback.format_exc()}"

            agent_error(error_message, simple_text=self.simple_text)

            return {
                "type": "tool_result",
                "tool_use_id": tool_call["id"],
                "content": [{"type": "text", "text": error_message}],
            }

    async def run_loop(self, llm_fn: callable) -> None:
        """
        Main agent loop orchestrator.
        
        Coordinates the execution phases: LLM → Tools → Loop Control → Repeat
        
        This method has been refactored for clarity and maintainability:
        - Complexity reduced from 41 to ~8
        - Duplication eliminated (50 lines removed)
        - Each phase is self-contained and testable
        
        :param llm_fn: The LLM function to call with messages.
        """
        print(f"\n{HELP_MESSAGE}")
        msg = self.user_input()
        if msg is None:
            return
        
        # Reset iteration counter for new task
        self.current_iteration = 0
        self.tool_call_history.clear()
        
        while True:
            # Increment iteration at the start of each cycle
            self.current_iteration += 1
            
            # Phase 1: Execute LLM
            was_interrupted, llm_result = await self.execute_llm_phase(llm_fn, msg)
            if was_interrupted:
                msg = self.user_input()
                if msg is None:
                    return
                # Reset counter and history for new user request
                self.current_iteration = 0
                self.tool_call_history.clear()
                continue
            
            response, tool_calls = llm_result
            agent_reply(f"💬 Agent: {response}", simple_text=self.simple_text)
            
            # Phase 2: Execute tools (if any)
            if tool_calls:
                was_interrupted, tool_results = await self.execute_tools_phase(tool_calls)
                if was_interrupted:
                    msg = self.user_input()
                    if msg is None:
                        return
                    # Reset counter and history for new user request
                    self.current_iteration = 0
                    self.tool_call_history.clear()
                    continue
                
                # Check loop control AFTER tool execution
                should_stop, stop_reason, user_prompt = should_continue_iteration(
                    self.current_iteration,
                    self.max_iterations,
                    response,
                    tool_calls,
                    self.tool_call_history,
                )
                
                if should_stop:
                    # Handle stop decision
                    if user_prompt and self.prompt_on_completion:
                        if not ask_continue(user_prompt, self.simple_text):
                            agent_info(f"🛑 Stopping: {stop_reason}", simple_text=self.simple_text)
                            return
                        else:
                            agent_info("▶️  Continuing at user request...", simple_text=self.simple_text)
                            self.current_iteration = 0
                            self.tool_call_history.clear()
                            msg = self.user_input()
                            if msg is None:
                                return
                    else:
                        agent_error(f"🛑 Stopping: {stop_reason}", simple_text=self.simple_text)
                        return
                else:
                    # Continue with tool results
                    msg = tool_results
            else:
                # No tool calls - check for completion and get next user input
                next_msg = await self.handle_loop_control_decision(response, tool_calls)
                if next_msg is None:
                    return  # Stop
                msg = next_msg


def load_loop_config() -> dict:
    """
    Load agent loop configuration from environment variables with defaults.
    Returns dict with max_iterations and prompt_on_completion.
    """
    max_iterations = int(os.getenv("MAX_ITERATIONS", str(DEFAULT_MAX_ITERATIONS)))
    prompt_on_completion = os.getenv("PROMPT_ON_COMPLETION", "true").lower() == "true"
    
    return {
        "max_iterations": max_iterations,
        "prompt_on_completion": prompt_on_completion,
    }


def create_llm() -> callable:
    """
    Create and return the LLM function using Anthropic, OpenAI, or custom provider, depending on environment variables.
    """
    # Read and validate provider preference
    preferred_provider = os.getenv("AI_PROVIDER", "anthropic").lower()
    if preferred_provider not in ("anthropic", "openai", "custom"):
        raise ValueError(
            f"Invalid AI_PROVIDER: {preferred_provider}. Must be 'anthropic', 'openai', or 'custom'."
        )

    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    anthropic_model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    openai_key = os.getenv("OPENAI_API_KEY")
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4o")
    custom_base_url = os.getenv("CUSTOM_BASE_URL")
    custom_api_key = os.getenv("CUSTOM_API_KEY")
    custom_model = os.getenv("CUSTOM_MODEL", "gpt-4o")

    temperature = os.getenv("AI_TEMPERATURE", 0.7)

    # make sure that temperature is a float
    temperature = float(temperature)

    # Debug output
    print(f"🔧 [Config] AI_PROVIDER={preferred_provider}")
    print(f"🔧 [Config] AI_TEMPERATURE={temperature}")
    print(f"🔧 [Config] ANTHROPIC_KEY={'✓' if anthropic_key else '✗'}")
    print(f"🔧 [Config] OPENAI_KEY={'✓' if openai_key else '✗'}")
    if preferred_provider == "custom":
        print(f"🔧 [Config] CUSTOM_BASE_URL={custom_base_url if custom_base_url else '✗'}")
        print(f"🔧 [Config] CUSTOM_API_KEY={'✓' if custom_api_key else '✗'}")
        print(f"🔧 [Config] CUSTOM_MODEL={custom_model}")

    # Use the preferred provider and validate its API key
    if preferred_provider == "anthropic":
        if not anthropic_key:
            raise EnvironmentError(
                f"AI_PROVIDER is set to 'anthropic' but ANTHROPIC_API_KEY is not set. "
                f"Please set ANTHROPIC_API_KEY or change AI_PROVIDER to 'openai' or 'custom'."
            )
        print(f"✅ [Provider] Using: Anthropic")
        return create_anthropic_llm(anthropic_model, anthropic_key, temperature)
    elif preferred_provider == "openai":
        if not openai_key:
            raise EnvironmentError(
                f"AI_PROVIDER is set to 'openai' but OPENAI_API_KEY is not set. "
                f"Please set OPENAI_API_KEY or change AI_PROVIDER to 'anthropic' or 'custom'."
            )
        print(f"✅ [Provider] Using: OpenAI")
        return create_openai_llm(openai_model, openai_key, temperature)
    elif preferred_provider == "custom":
        if not custom_api_key:
            raise EnvironmentError(
                f"AI_PROVIDER is set to 'custom' but CUSTOM_API_KEY is not set. "
                f"Please set CUSTOM_API_KEY or change AI_PROVIDER to 'anthropic' or 'openai'."
            )
        if not custom_base_url:
            raise EnvironmentError(
                f"AI_PROVIDER is set to 'custom' but CUSTOM_BASE_URL is not set. "
                f"Please set CUSTOM_BASE_URL or change AI_PROVIDER to 'anthropic' or 'openai'."
            )
        print(f"✅ [Provider] Using: Custom OpenAI-Compatible ({custom_base_url})")
        return create_openai_llm(custom_model, custom_api_key, temperature, base_url=custom_base_url)


async def agent_main() -> None:
    """
    Main async entrypoint: parses arguments, registers tools, and runs the agent loop.
    Handles graceful exit and cancellation.
    """
    try:
        # Display welcome message as the first thing
        display_welcome_message()

        # Display custom tools if any are loaded
        display_custom_tools()

        async with AsyncExitStack() as exit_stack:
            parser = argparse.ArgumentParser(description="Agent Loop")
            parser.add_argument(
                "--debug", action="store_true", help="Show tool input/output"
            )
            parser.add_argument(
                "--safe",
                action="store_true",
                help="Require confirmation before executing tools",
            )
            parser.add_argument(
                "--simple-text",
                "-s",
                action="store_true",
                help="Use plain text output instead of Rich formatting",
            )
            parser.add_argument(
                "--max-iterations",
                type=int,
                help=f"Maximum agent iteration cycles (default: {DEFAULT_MAX_ITERATIONS})",
            )
            parser.add_argument(
                "--no-prompt-on-completion",
                action="store_true",
                help="Disable prompting when completion is detected (auto-stop instead)",
            )
            args = parser.parse_args()
            
            # Load configuration with CLI args taking priority
            loop_config = load_loop_config()
            max_iterations = args.max_iterations if args.max_iterations else loop_config["max_iterations"]
            prompt_on_completion = not args.no_prompt_on_completion and loop_config["prompt_on_completion"]

            # Start spinner for MCP loading
            mcp_spinner = Halo(text="🔌 Loading MCP servers...", spinner="dots")
            mcp_spinner.start()

            try:
                mcp_count = await mcp_manager.register_tools(
                    exit_stack, debug=args.debug
                )
                mcp_spinner.stop()  # Stop spinner on success
                if mcp_count > 0:
                    print(f"✅ [MCP] Loaded {mcp_count} MCP tool(s)")
                else:
                    print("ℹ️ [MCP] No MCP tools configured")
            except Exception as e:
                mcp_spinner.stop()  # Stop spinner on error
                error_msg = f"❌ [MCP Error] Failed to load MCP servers: {type(e).__name__}: {str(e)}"
                if args.debug:
                    import traceback

                    error_msg += f"\n\nMCP Error Stack trace:\n{traceback.format_exc()}"
                print(error_msg)
                print("⚠️  Continuing without MCP servers...")

            loop_obj = asyncio.get_event_loop()
            agent = AgentLoop(
                debug=args.debug,
                safe=args.safe,
                simple_text=args.simple_text,
                max_iterations=max_iterations,
                prompt_on_completion=prompt_on_completion,
            )
            
            # Display configuration
            print(f"🔧 [Config] MAX_ITERATIONS={max_iterations}")
            print(f"🔧 [Config] PROMPT_ON_COMPLETION={prompt_on_completion}")
            
            setup_signal_handlers(loop_obj, agent.interrupt_event)
            await agent.run_loop(create_llm())
        print("\n👋 Goodbye!")
    except GracefulExit:
        print("\n👋 Goodbye!")
    except asyncio.CancelledError:
        print("\n⚠️  Operation cancelled")
    except Exception as e:
        error_type = type(e).__name__
        error_msg = f"\n❌ [Agent Error] {error_type}: {str(e)}"

        # Special handling for ExceptionGroup/TaskGroup errors
        if hasattr(e, "exceptions"):
            error_msg += f"\n\n📋 Exception Group Details:"
            for i, sub_exc in enumerate(e.exceptions, 1):
                error_msg += f"\n  {i}. {type(sub_exc).__name__}: {str(sub_exc)}"

        import traceback

        error_msg += f"\n\n🔍 Stack Trace:\n{traceback.format_exc()}"
        print(error_msg)
        raise


def main() -> None:
    """
    Synchronous entrypoint for the agent-loop CLI application.
    Runs the async agent_main and handles top-level exceptions.
    """
    try:
        asyncio.run(agent_main())
    except GracefulExit:
        print("\n👋 Goodbye!")
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user. Goodbye!")
    except Exception as e:
        error_type = type(e).__name__
        error_msg = f"\n❌ [Critical Error] {error_type}: {str(e)}"

        # Special handling for ExceptionGroup/TaskGroup errors at top level
        if hasattr(e, "exceptions"):
            error_msg += f"\n\n📋 Exception Group Details:"
            for i, sub_exc in enumerate(e.exceptions, 1):
                error_msg += f"\n  {i}. {type(sub_exc).__name__}: {str(sub_exc)}"

        # Always show stack trace for critical errors
        import traceback

        error_msg += f"\n\n🔍 Stack Trace:\n{traceback.format_exc()}"
        error_msg += f"\n\n💡 If this error persists, please run with --debug for more information."

        print(error_msg)


if __name__ == "__main__":
    main()
