from langchain_core.messages import AIMessage, SystemMessage, BaseMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.graph import MessagesState, StateGraph, START, END
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.sqlite import SqliteSaver
from langchain_openai import ChatOpenAI
from langmem import create_manage_memory_tool, create_search_memory_tool

from typing import Literal, List, Dict
import streamlit as st
from datetime import datetime
from ddgs import DDGS
import requests
from readability import Document
from bs4 import BeautifulSoup

# Add State class definition
class State(MessagesState):
    summary: str

def call_model(state,react_agent_executor):
    summary = state.get("summary", "")
    current_messages = state["messages"]
    last_messages_to_send = current_messages[-5:] # Send last 5 actual messages

    messages_for_react_agent_input: List[BaseMessage] = []
    if summary:
    # The summary is prepended as a system message for context
        system_message_content = f"Here is a summary of the conversation so far: {summary}. Use this to inform your response."
        messages_for_react_agent_input.append(SystemMessage(content=system_message_content))

    messages_for_react_agent_input.extend(last_messages_to_send)

    # The ReAct agent expects input in the format {"messages": [list_of_messages]}
    # The last message in this list should be the one it needs to respond to (typically HumanMessage).
    agent_input = {"messages": messages_for_react_agent_input}

    # Invoke the ReAct agent.
    # Since there are no tools, it will essentially be an LLM call structured by the ReAct framework.
    # The react_agent_executor is already compiled.
    response_dict = react_agent_executor.invoke(agent_input)

    # The ReAct agent's response (AIMessage) will be in the 'messages' key of the output dictionary.
    # It's a list, and the agent's response is typically the last message added.
    ai_response_message = response_dict["messages"][-1]

    if not isinstance(ai_response_message, AIMessage):
        # Fallback or error handling if the last message isn't an AIMessage
        # This shouldn't happen in a typical ReAct flow without tool errors.
        st.error("ReAct agent did not return an AIMessage as expected.")
        return {"messages": [AIMessage(content="Sorry, I encountered an issue.")]}

    # The graph will automatically append the response to state["messages"]
    # We just need to return the new message to be added
    return {"messages": [ai_response_message]}

def summarize_conversation(state,chat_model):
        summary = state.get("summary", "")
        current_messages = state["messages"]
        # Let's use more messages for a better summary, e.g., last 6 (3 turns) that led to summarization
        # The last two messages are the AI response that triggered the summary, and the user message before that.
        # We want to summarize the conversation *before* the current turn that might be too long.
        messages_to_summarize = current_messages[:-2] # Exclude the last AI response and user query that triggered summary
        if len(messages_to_summarize) > 10 : # Cap the number of messages to summarize to avoid large prompts
            messages_to_summarize = messages_to_summarize[-10:]


        if not messages_to_summarize: # Nothing to summarize yet (e.g., if called too early)
            return {"summary": summary, "messages": []}


        summary_prompt_parts = []
        if summary:
            summary_prompt_parts.append(f"This is the current summary of the conversation: {summary}\n")

        summary_prompt_parts.append("Based on the following recent messages:\n")
        for msg in messages_to_summarize:
            if isinstance(msg, HumanMessage):
                summary_prompt_parts.append(f"User: {msg.content}")
            elif isinstance(msg, AIMessage):
                summary_prompt_parts.append(f"Assistant: {msg.content}")

        summary_prompt_parts.append("\nPlease update or create a concise summary of the entire conversation.")

        final_summary_prompt = "\n".join(summary_prompt_parts)

        # Construct messages for summarization
        messages_for_summary_llm = [HumanMessage(content=final_summary_prompt)]

        response = chat_model.invoke(messages_for_summary_llm)
        return {"summary": response.content, "messages": []}

def should_continue(state) -> Literal["summarize_conversation", END]: # type: ignore
    messages = state["messages"]
    # Trigger summary if there are more than 6 messages (e.g., 3 user, 3 AI + 1 new user = 7 messages)
    # The summarization will happen *after* the AI responds to the current user message.
    if len(messages) > 6: 
        return "summarize_conversation"
    return END

def create_graph(model, api_key, base_url, conn, store, user_id, web_search_enabled=False, search_method_rag=True, num_results=5):
    """Create and compile the LangGraph workflow"""
    
    # Initialize models
    chat_model = ChatOpenAI(
        temperature=0,
        model=model,
        api_key=api_key,
        base_url=base_url
    )
    
    # Create web search tool
    @tool
    def search_web(query: str, timelimit: Literal["d", "w", "m", "y"] = "w") -> str:
        """Search the web for information about a topic.
        
        Args:
            query: The search query
            timelimit: Time limit - "d" (day), "w" (week), "m" (month), "y" (year)
        
        Returns:
            Search results from the web
        """
        results = DDGS().text(query, max_results=num_results,
                            timelimit=timelimit,
                            )
        timestamp = datetime.now().isoformat()
        namespace = ("web_search", user_id)

        for i, result in enumerate(results):
            key = f"{query}_{timestamp}_{i}"
            value = {
                "query": query,
                "title": result.get("title", ""),
                "href": result.get("href", ""),
                "body": result.get("body", ""),
                "timestamp": timestamp,
                "text": f"{result.get('title', '')} {result.get('body', '')}"
            }
            # Store with indexing for vector search
            store.put(
                namespace=namespace,
                key=key,
                value=value,
                index=["text"]  # Index the text field for vector search
            )
        if search_method_rag:
            final_results = store.search(
            namespace,
            query=query,
            limit=1
            )
        else:
            final_results = results

        return final_results
    
    @tool
    def fetch_url_content(urls: List[str], timeout: int = 10) -> List[Dict[str, str]]:
        """
        Fetch and extract the main text and title from each URL.

        Args:
            urls: A list of page URLs to retrieve.
            timeout: HTTP timeout in seconds.

        Returns:
            A list of dicts with keys: 'url', 'title', 'content'.
        """
        results = []
        for url in urls:
            try:
                resp = requests.get(url, timeout=timeout)
                resp.raise_for_status()
                doc = Document(resp.text)
                title = doc.title()
                html = doc.summary()
                soup = BeautifulSoup(html, 'html.parser')
                text = soup.get_text(separator='\n').strip()
                results.append({"url": url, "title": title, "content": text})
            except Exception as e:
                # Optionally include an error field:
                results.append({"url": url, "error": str(e)})
        return results

    # Create memory tools
    manage_memory_tool = create_manage_memory_tool(
        store=store, 
        namespace=("memory", user_id), 
        instructions="Store any interests and topics the user talks about."
    )
    search_memory_tool = create_search_memory_tool(
        store=store, 
        namespace=("memory", user_id), 
        instructions="Search and recall stored information about the user, including their name, interests, preferences, and any topics they've discussed."
    )
    
    # Build tools list
    tools = [manage_memory_tool, search_memory_tool, fetch_url_content]
    if web_search_enabled:
        tools.append(search_web)

    # Base prompt
    prompt_content = (
        "You are a helpful assistant with memory capabilities. "
        "IMPORTANT: Before answering any question about the user or past conversations, "
        "you MUST first use the search_memory tool to check if you have any stored information. "
        "Store any new information about the user using the manage_memory tool. "
        "When URLs are provided directly by the user, use the fetch_url_content tool "
        "to retrieve full page contents so your answers can be grounded in that text."
    )

    # Extend if web search is enabled
    if web_search_enabled:
        prompt_content += (
            " You can also search the web for current information when needed using the search_web tool. "
            f"Decide on the timelimit argument based on the query of the user. Try to get updated results "
            f"based on the current datetime (which is {datetime.now()}). "
            "use the fetch_url_content tool to retrieve full page contents if you need to get more information a single or multiple pages from the web pages you got from web search results"
        )

    # Create react agent
    react_agent_executor = create_react_agent(
        model=chat_model,
        tools=tools,
        prompt=SystemMessage(prompt_content),
        store=store
    )
    
    # Create node functions
    def call_model_node(state):
        return call_model(state, react_agent_executor)
    
    def summarize_conversation_node(state):
        return summarize_conversation(state, chat_model)
    
    # Build workflow
    workflow = StateGraph(State)
    workflow.add_node("conversation", call_model_node)
    workflow.add_node("summarize_conversation", summarize_conversation_node)
    workflow.add_edge(START, "conversation")
    workflow.add_conditional_edges("conversation", should_continue)
    workflow.add_edge("summarize_conversation", END)
    
    # Compile with checkpointer
    checkpointer = SqliteSaver(conn=conn)
    app = workflow.compile(checkpointer=checkpointer, store=store)
    
    return app, checkpointer
