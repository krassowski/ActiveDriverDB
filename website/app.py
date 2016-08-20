from flask import Flask
from flask_assets import Bundle
from flask_assets import Environment
from flask_login import LoginManager
from database import db


app = Flask(__name__)
app.config.from_pyfile('config.py')

db.app = app
db.init_app(app)
db.create_all(bind='__all__')

"""
Configure Login Manager
"""
login_manager = LoginManager()
login_manager.init_app(app)


"""
Define assets
"""
assets = Environment(app)

bundles = {
    'js_search': Bundle(
        'search.js',
        filters='rjsmin',
        output='min/search.js'
    ),
    'js_protein_view': Bundle(
        'common.js',
        'needleplot.js',
        'filters.js',
        'tracks.js',
        filters='rjsmin',
        output='min/proteinView.js'
    ),
    'js_network_view': Bundle(
        'common.js',
        'orbits.js',
        'network.js',
        'filters.js',
        filters='rjsmin',
        output='min/networkView.js'
    ),
    'css_common': Bundle(
        'sass/style.css',
        filters='cssutils',
        output='min/style.css'
    ),
    'css_network': Bundle(
        'sass/network.css',
        'sass/filters.css',
        filters='cssutils',
        output='min/network.css'
    ),
    'css_protein': Bundle(
        'sass/protein.css',
        'sass/tracks.css',
        'sass/filters.css',
        filters='cssutils',
        output='min/protein.css'
    ),
    'css_search': Bundle(
        'sass/search.css',
        filters='cssutils',
        output='min/search.css'
    ),
    'css_page': Bundle(
        'sass/page.css',
        filters='cssutils',
        output='min/page.css'
    )
}

for name, bundle in bundles.items():
    assets.register(name, bundle)


"""
Register functions for Jinja
"""

import csrf
import json

app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True

app.jinja_env.globals['csrf_token'] = csrf.new_csrf_token

app.jinja_env.filters['json'] = json.dumps

"""
Import viwes
"""

# allow acces to this app from views through module
import sys
sys.path.insert(0, '..')

from website.views import general, ProteinView, SearchView, NetworkView
from website.views import ContentManagmentSystem

app.register_blueprint(general)

ProteinView.register(app)
NetworkView.register(app)
SearchView.register(app)
ContentManagmentSystem.register(app)
