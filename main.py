import os
import json
import re
import openai
import requests
from bs4 import BeautifulSoup
from graphviz import Digraph
import networkx as nx
from neo4j import GraphDatabase
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv
import instructor 
from models import KnowledgeGraph
import time

instructor.patch()

load_dotenv()

app = Flask(__name__)

# Set your OpenAI API key
openai.api_key = os.getenv("OPENAI_API_KEY")
response_data = ""

# If Neo4j credentials are set, then Neo4j is used to store information
neo4j_username = os.environ.get("NEO4J_USERNAME")
neo4j_password = os.environ.get("NEO4J_PASSWORD")
neo4j_url = os.environ.get("NEO4J_URL")
neo4j_driver = None

if neo4j_username and neo4j_password and neo4j_url:
    neo4j_driver = GraphDatabase.driver(
        neo4j_url, auth=(neo4j_username, neo4j_password))
    with neo4j_driver.session() as session:
        session.run("RETURN 1")
        print("Neo4j database connected successfully!")

# Function to scrape text from a website


def scrape_text_from_url(url):
    response = requests.get(url)
    if response.status_code != 200:
        return "Error: Could not retrieve content from URL."
    soup = BeautifulSoup(response.text, "html.parser")
    paragraphs = soup.find_all("p")
    text = " ".join([p.get_text() for p in paragraphs])
    print("web scrape done")
    return text

# Check sub/user plan before making a request
def make_request(request_body):
    if check_if_free_plan():
        time.sleep(20)
    response = api.create_completion(request_body)
    return response

# Function to check user plan
def check_if_free_plan():
    return user_plan == 'free'

# Rate limiting
@app.after_request
def add_header(response):
    if check_if_free_plan():
        response.headers['Retry-After'] = 20
    return response

# Handling 429 error
@app.errorhandler(429) 
def too_many_requests(e):
    time.sleep(20)
    return make_request(request.json)

def correct_json(response_data):
    """
    Corrects the JSON response from OpenAI to be valid JSON
    """
    response_data = re.sub(
        r',\s*}', '}',
        re.sub(r',\s*]', ']',
               re.sub(r'(\w+)\s*:', r'"\1":', response_data)))
    return response_data


@app.route("/get_response_data", methods=["POST"])
def get_response_data():
    global response_data
    user_input = request.json.get("user_input", "")
    if not user_input:
        return jsonify({"error": "No input provided"}), 400
    print("starting openai call")
    try:
        completion: KnowledgeGraph = openai.ChatCompletion.create(
            model="gpt-3.5-turbo-16k",
            messages=[
                {
                    "role": "user",
                    "content": f"Help me understand following by describing as a detailed knowledge graph: {user_input}",
                }
            ],
            response_model=KnowledgeGraph,
        ) # type: ignore

        # Its now a dict, no need to worry about json loading so many times 
        response_data = completion.model_dump()
    except openai.error.RateLimitError as e:
        # request limit exceeded or something.
        print(e)
        return jsonify({"error": "".format(e)}), 429
    except Exception as e:
        # general exception handling
        print(e)
        return jsonify({"error": "".format(e)}), 400


    response_data = completion.model_dump()
    total_tokens_used = completion.usage["total_tokens"]
    print(f"total_tokens_used: {total_tokens_used}")
    response_data = correct_json(response_data)
    
    # print(response_data)

    try:
        if neo4j_driver:
            # Import nodes
            neo4j_driver.execute_query("""
            UNWIND $nodes AS node
            MERGE (n:Node {id:toLower(node.id)})
            SET n.type = node.type, n.label = node.label, n.color = node.color""",
                                       {"nodes": response_data['nodes']})
            # Import relationships
            neo4j_driver.execute_query("""
            UNWIND $rels AS rel
            MATCH (s:Node {id: toLower(rel.from)})
            MATCH (t:Node {id: toLower(rel.to)})
            MERGE (s)-[r:RELATIONSHIP {type:rel.relationship}]->(t)
            SET r.direction = rel.direction,

                r.color = rel.color,
                r.timestamp = timestamp();
            """, {"rels": json.loads(response_data)['edges']})

    except json.decoder.JSONDecodeError as jde:
        return jsonify({"Error": "{}".format(jde)}), 500

    return response_data, 200


# Function to visualize the knowledge graph using Graphviz
@app.route("/graphviz", methods=["POST"])
def visualize_knowledge_graph_with_graphviz():
    global response_data
    dot = Digraph(comment="Knowledge Graph")
    response_dict = response_data
    # Add nodes to the graph
    for node in response_dict.get("nodes", []):
        dot.node(node["id"], f"{node['label']} ({node['type']})")

    # Add edges to the graph
    for edge in response_dict.get("edges", []):
        dot.edge(edge["from"], edge["to"], label=edge["relationship"])

    # Render and visualize
    dot.render("knowledge_graph.gv", view=False)
    # Render to PNG format and save it
    dot.format = "png"
    dot.render("static/knowledge_graph", view=False)

    # Construct the URL pointing to the generated PNG
    png_url = f"{request.url_root}static/knowledge_graph.png"

    return jsonify({"png_url": png_url}), 200


@app.route("/get_graph_data", methods=["POST"])
def get_graph_data():
    try:
        if neo4j_driver:
            nodes, _, _ = neo4j_driver.execute_query("""
            MATCH (n)
            WITH collect(
                {data: {id: n.id, label: n.label, color: n.color}}) AS node
            RETURN node
            """)
            nodes = [el['node'] for el in nodes][0]

            edges, _, _ = neo4j_driver.execute_query("""
            MATCH (s)-[r]->(t)
            WITH collect(
                {data: {source: s.id, target: t.id, label:r.type, color: r.color}}
            ) AS rel
            RETURN rel
            """)
            edges = [el['rel'] for el in edges][0]
        else:
            global response_data
            # print(response_data)
            response_dict = response_data
            # Assume response_data is global or passed appropriately
            nodes = [
                {
                    "data": {
                        "id": node["id"],
                        "label": node["label"],
                        "color": node.get("color", "defaultColor"),
                    }
                }
                for node in response_dict["nodes"]
            ]
            edges = [
                {
                    "data": {
                        "source": edge["from_"],
                        "target": edge["to"],
                        "label": edge["relationship"],
                        "color": edge.get("color", "defaultColor"),
                    }
                }
                for edge in response_dict["edges"]
            ]
        return jsonify({"elements": {"nodes": nodes, "edges": edges}})
    except:
        return jsonify({"elements": {"nodes": [], "edges": []}})


@app.route("/get_graph_history", methods=["GET"])
def get_graph_history():
    try:
        page = request.args.get('page', default=1, type=int)
        per_page = 10
        skip = (page - 1) * per_page

        if neo4j_driver:
            # Getting the total number of graphs
            total_graphs, _, _ = neo4j_driver.execute_query("""
            MATCH (n)-[r]->(m)
            RETURN count(n) as total_count
            """)
            total_count = total_graphs[0]['total_count']

            # Fetching 10 most recent graphs
            result, _, _ = neo4j_driver.execute_query("""
            MATCH (n)-[r]->(m)
            RETURN n, r, m
            ORDER BY r.timestamp DESC
            SKIP {skip}
            LIMIT {per_page}
            """.format(skip=skip, per_page=per_page))

            # Process the 'result' to format it as a list of graphs
            graph_history = [process_graph_data(record) for record in result]
            remaining = max(0, total_count - skip - per_page)

            return jsonify({"graph_history": graph_history, "remaining": remaining})
        else:
            return jsonify({"error": "Neo4j driver not initialized"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def process_graph_data(record):
    """
    This function processes a record from the Neo4j query result
    and formats it as a dictionary with the node details and the relationship.

    :param record: A record from the Neo4j query result
    :return: A dictionary representing the graph data
    """
    try:
        node_from = record['n'].items()
        node_to = record['m'].items()
        relationship = record['r'].items()

        graph_data = {
            "from_node": {key: value for key, value in node_from},
            "to_node": {key: value for key, value in node_to},
            "relationship": {key: value for key, value in relationship},
        }

        return graph_data
    except Exception as e:
        return {"error": str(e)}


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
