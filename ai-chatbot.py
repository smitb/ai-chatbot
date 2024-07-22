#!/usr/bin/env python3
import logging
from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_openai import ChatOpenAI
from redis_saver import RedisSaver, initialize_sync_pool
from dotenv import load_dotenv
from uuid import uuid4
import os


load_dotenv(".env")

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
LOG_LEVEL = logging.INFO


logger = logging.getLogger(__name__)
logging.basicConfig(level=LOG_LEVEL)

llm = ChatOpenAI(model=MODEL)
logger.info(f"Using model: {MODEL}")


class State(TypedDict):
    messages: Annotated[list, add_messages]


def chatbot(state: State):
    return {"messages": [llm.invoke(state["messages"])]}


def main():
    graph_builder = StateGraph(State)
    graph_builder.add_node("chatbot", chatbot)
    graph_builder.add_edge(START, "chatbot")
    graph_builder.add_edge("chatbot", END)

    sync_pool = initialize_sync_pool(host=os.environ.get('REDIS_HOST', 'localhost'), port=os.environ.get('REDIS_PORT', 6379), db=0)
    memory = RedisSaver(sync_connection=sync_pool)

    graph = graph_builder.compile(checkpointer=memory)

    config = {"configurable": {"thread_id": str(uuid4())}}

    while True:
        user_input = ''
        try:
            user_input = input(":> ")
        except EOFError:
            # CTRL-d
            print("Goodbye!")
            break

        if user_input.lower() in ["quit", "exit", "q"]:
            print("Goodbye!")
            break

        for event in graph.stream({"messages": ("user", user_input)}, config):
            for value in event.values():
                print("->", value["messages"][-1].content)


if __name__ == "__main__":
    main()
