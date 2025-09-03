from collections.abc import Sequence
from inspect import signature
from typing import Any, Callable, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tools import BaseTool

from langgraph.agent.types import (
    AgentJump,
    AgentMiddleware,
    AgentState,
    AgentUpdate,
    JumpTo,
    ModelRequest,
    ResponseFormat,
)
from langgraph.constants import END, START
from langgraph.graph.state import StateGraph
from langgraph.prebuilt.tool_node import ToolNode


def create_agent(
    *,
    model: str | BaseChatModel,
    tools: Sequence[BaseTool | Callable] | ToolNode,
    system_prompt: str,
    middleware: Sequence[AgentMiddleware] = (),
    response_format: ResponseFormat | None = None,
    context_schema: type[Any] | None = None,
) -> StateGraph[AgentState, None, AgentUpdate]:
    # init chat model
    if isinstance(model, str):
        try:
            from langchain.chat_models import (  # type: ignore[import-not-found]
                init_chat_model,
            )
        except ImportError:
            raise ImportError(
                "Please install langchain (`pip install langchain`) to "
                "use '<provider>:<model>' string syntax for `model` parameter."
            )

        model = cast(BaseChatModel, init_chat_model(model))

    # init tool node
    tool_node = tools if isinstance(tools, ToolNode) else ToolNode(tools=tools)

    # validate middleware
    assert len({m.__class__.__name__ for m in middleware}) == len(middleware), (
        "Please remove duplicate middleware instances."
    )  # this is just to keep the node names simple, we can change if needed
    middleware_w_before = [
        m
        for m in middleware
        if m.__class__.before_model is not AgentMiddleware.before_model
    ]
    middleware_w_modify_model_request = [
        m
        for m in middleware
        if m.__class__.modify_model_request is not AgentMiddleware.modify_model_request
    ]
    middleware_w_after = [
        m
        for m in middleware
        if m.__class__.after_model is not AgentMiddleware.after_model
    ]

    default_model_request = ModelRequest(
        model=model, 
        tools=list(tool_node.tools_by_name.values()), 
        system_prompt=system_prompt, 
        response_format=response_format,
        messages=[],
        tool_choice=None,
    )

    # create graph, add nodes
    graph = StateGraph(
        AgentState,
        input_schema=AgentUpdate,
        output_schema=AgentUpdate,
        context_schema=context_schema,
    )

    def model_request(state: AgentState) -> AgentState:

        request = state.model_request or default_model_request

        # prepare messages
        if request.system_prompt:
            messages = [SystemMessage(request.system_prompt)] + request.messages
        else:
            messages = request.messages
        # call model
        if request.response_format:
            model_ = request.model.with_structured_output(
                request.response_format, include_raw=True
            )
            output = model_.invoke(
                messages, tools=request.tools, tool_choice=request.tool_choice
            )
            return {"messages": output["raw"], "response": output["parsed"]}
        else:
            model_ = request.model
            output = model_.invoke(
                messages, tools=request.tools, tool_choice=request.tool_choice
            )
            if state.response is not None:
                return {"messages": output, "response": None}
            else:
                return {"messages": output}

    graph.add_node("model_request", model_request)
    graph.add_node("tools", tool_node)

    for m in middleware:
        if m.__class__.before_model is not AgentMiddleware.before_model:
            graph.add_node(
                f"{m.__class__.__name__}.before_model",
                m.before_model,
                input_schema=m.State,
            )
        if m.__class__.modify_model_request is not AgentMiddleware.modify_model_request:

            def modify_model_request_node(state: AgentState) -> dict[str, ModelRequest]:
                # TODO assert request.tools in tools, or pass them to tool node
                return {"model_request": m.modify_model_request(state.model_request or default_model_request, state)}
        
            graph.add_node(
                f"{m.__class__.__name__}.modify_model_request",
                modify_model_request_node,
                input_schema=m.State,
            )

        if m.__class__.after_model is not AgentMiddleware.after_model:
            graph.add_node(
                f"{m.__class__.__name__}.after_model",
                m.after_model,
                input_schema=m.State,
            )

    # add start edge
    first_node = (
        f"{middleware_w_before[0].__class__.__name__}.before_model"
        if middleware_w_before
        else f"{middleware_w_modify_model_request[0].__class__.__name__}.modify_model_request"
        if middleware_w_modify_model_request
        else "model_request"
    )
    last_node = (
        f"{middleware_w_after[0].__class__.__name__}.after_model"
        if middleware_w_after
        else "model_request"
    )
    graph.add_edge(START, first_node)

    # add cond edges
    graph.add_conditional_edges(
        "tools",
        _make_tools_to_model_edge(tool_node, first_node),
        [first_node, END],
    )
    graph.add_conditional_edges(
        last_node, _make_model_to_tools_edge(first_node), ["tools", END]
    )

    # add before model edges
    if middleware_w_before:
        for m1, m2 in zip(middleware_w_before, middleware_w_before[1:]):
            _add_middleware_edge(
                graph,
                m1.before_model,
                f"{m1.__class__.__name__}.before_model",
                f"{m2.__class__.__name__}.before_model",
                first_node,
            )
        _add_middleware_edge(
            graph,
            middleware_w_before[-1].before_model,
            f"{middleware_w_before[-1].__class__.__name__}.before_model",
            "model_request",
            first_node,
        )

    # add modify model request edges
    if middleware_w_modify_model_request:
        for m1, m2 in zip(middleware_w_modify_model_request, middleware_w_modify_model_request[1:]):
            _add_middleware_edge(
                graph,
                m1.modify_model_request,
                f"{m1.__class__.__name__}.modify_model_request",
                f"{m2.__class__.__name__}.modify_model_request",
                first_node,
            )
        _add_middleware_edge(
            graph,
            middleware_w_modify_model_request[-1].modify_model_request,
            f"{middleware_w_modify_model_request[-1].__class__.__name__}.modify_model_request",
            "model_request",
            first_node,
        )

    # add after model edges
    if middleware_w_after:
        graph.add_edge(
            "model_request", f"{middleware_w_after[-1].__class__.__name__}.after_model"
        )
        for idx in range(len(middleware_w_after) - 1, 0, -1):
            m1 = middleware_w_after[idx]
            m2 = middleware_w_after[idx - 1]
            _add_middleware_edge(
                graph,
                m1.after_model,
                f"{m1.__class__.__name__}.after_model",
                f"{m2.__class__.__name__}.after_model",
                first_node,
            )

    return graph


def _resolve_jump(jump_to: JumpTo | None, first_node: str) -> str | None:
    if jump_to == "model":
        return first_node
    elif jump_to:
        return jump_to


def _make_model_to_tools_edge(first_node: str) -> Callable[[AgentState], str | None]:
    def model_to_tools(state: AgentState) -> str | None:
        if state.jump_to:
            return _resolve_jump(state.jump_to, first_node)
        message = state.messages[-1]
        if isinstance(message, AIMessage) and message.tool_calls:
            return "tools"

        return END

    return model_to_tools


def _make_tools_to_model_edge(
    tool_node: ToolNode, next_node: str
) -> Callable[[AgentState], str | None]:
    def tools_to_model(state: AgentState) -> str | None:
        ai_message = [m for m in state.messages if isinstance(m, AIMessage)][-1]
        if all(
            tool_node.tools_by_name[c["name"]].return_direct
            for c in ai_message.tool_calls
            if c["name"] in tool_node.tools_by_name
        ):
            return END

        return next_node

    return tools_to_model


def _add_middleware_edge(
    graph: StateGraph,
    method: Callable[[AgentState], AgentUpdate | AgentJump | None],
    name: str,
    default_destination: str,
    model_destination: str,
) -> None:
    sig = signature(method)
    uses_jump = sig.return_annotation is AgentJump or AgentJump in getattr(
        sig.return_annotation, "__args__", ()
    )

    if uses_jump:

        def jump_edge(state: AgentState) -> str:
            return (
                _resolve_jump(state.jump_to, model_destination) or default_destination
            )

        destinations = [default_destination, END, "tools"]
        if name != model_destination:
            destinations.append(model_destination)

        graph.add_conditional_edges(name, jump_edge, destinations)
    else:
        graph.add_edge(name, default_destination)
