/*
   MCP bridge plugin for NetRadiant-custom
   Copyright (C) 2026 the NetRadiant-custom contributors

   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License as published by
   the Free Software Foundation; either version 2 of the License, or
   (at your option) any later version.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License for more details.

   You should have received a copy of the GNU General Public License
   along with this program; if not, write to the Free Software
   Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
 */

#include "mcpbridge.h"
#include "json.h"

#include "debugging/debugging.h"

#include "iplugin.h"

#include "string/string.h"
#include "stream/stringstream.h"
#include "modulesystem/singletonmodule.h"

#include "ibrush.h"        // brush creation
#include "icamera.h"       // camera get/set
#include "ieclass.h"       // entity class lookup
#include "ientity.h"       // entity keys
#include "ireference.h"    // map save / reload through the resource
#include "iscenegraph.h"
#include "iselection.h"
#include "iundo.h"
#include "mapfile.h"       // change counter, used as the scene revision
#include "preferencesystem.h"
#include "qerplugin.h"

#include "eclasslib.h"     // EntityClass::fixedsize; ieclass.h only forward-declares it
#include "scenelib.h"
#include "stringio.h"
#include "math/aabb.h"
#include "math/matrix.h"
#include "math/quaternion.h"

#include <QSocketNotifier>
#include <QTimer>
#include <QWidget>

#include <cstdlib>
#include <cstring>
#include <optional>
#include <string>
#include <vector>

#ifdef WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>
#endif


/// icamera.h declares the table but no module accessor. radiant registers it as
/// type "camera", version 1, name "*" (radiant/pluginapi.cpp), so the standard
/// six lines that every include/i*.h ends with are all that is missing.
typedef GlobalModule<_QERCameraTable> GlobalCameraModule;
typedef GlobalModuleRef<_QERCameraTable> GlobalCameraModuleRef;
inline _QERCameraTable& GlobalCamera(){
	return GlobalCameraModule::getTable();
}


namespace MCPBridge
{

QWidget* g_mainWindow = 0;

Settings& settings(){
	static Settings s_settings;
	return s_settings;
}


//  ***************
// ** scene access **
//  ***************

/// \brief True when a map root is inserted. iscenegraph.h documents
/// currentLayer() as returning 0 in exactly that case, and it is the only
/// non-asserting probe in the public interface. This matters because
/// ScreenUpdates_Disable() pumps the Qt event loop during map load, so a
/// request can arrive while the graph has no root.
inline bool map_open(){
	return GlobalSceneGraph().currentLayer() != 0;
}

inline MapFile* map_file(){
	return map_open() ? Node_getMapFile( GlobalSceneGraph().root() ) : 0;
}

/// \brief The undo-system change counter, used as an opaque scene revision.
/// Every UndoableCommand that records anything bumps it, which is exactly the
/// granularity at which ids below stop being meaningful.
inline std::size_t map_revision(){
	MapFile* mapfile = map_file();
	return mapfile != 0 ? mapfile->changes() : 0;
}

inline bool map_saved(){
	MapFile* mapfile = map_file();
	return mapfile == 0 || mapfile->saved();
}

inline void write_bounds( json::Writer& writer, const AABB& bounds ){
	if ( !aabb_valid( bounds ) ) {
		writer.null();
		return;
	}
	const Vector3 mins = bounds.origin - bounds.extents;
	const Vector3 maxs = bounds.origin + bounds.extents;
	writer.beginObject();
	writer.key( "mins" ).vector3( mins.data() );
	writer.key( "maxs" ).vector3( maxs.data() );
	writer.endObject();
}

inline AABB scene_bounds(){
	scene::Path path( makeReference( GlobalSceneGraph().root() ) );
	scene::Instance* instance = GlobalSceneGraph().find( path );
	return instance != 0 ? instance->worldAABB() : AABB();
}


/// \brief A snapshot of every entity and primitive, and the ids that name them.
///
/// Ids are positional: "e3" is the fourth entity, "e3.p7" the eighth primitive
/// of that entity. They are rebuilt from scratch on every request that needs
/// them - a graph walk is cheap next to a socket round trip, and a cache would
/// be one more thing that can point at a freed node.
///
/// Two things a client must know. The order is scene::Graph traversal order,
/// which CompiledGraph keys on node address, so it does *not* match the entity
/// order in the .map file. And any mutation renews the revision, after which
/// old ids may name different objects; every id-bearing reply carries the
/// revision it was built at, and requests may pin it.
class SceneIndex
{
public:
	struct Entity
	{
		scene::Path path;
		std::vector<scene::Path> primitives;
	};

	std::vector<Entity> m_entities;
	std::size_t m_revision = 0;

	class Walker : public scene::Graph::Walker
	{
		SceneIndex& m_index;
	public:
		Walker( SceneIndex& index ) : m_index( index ){
		}
		bool pre( const scene::Path& path, scene::Instance& instance ) const override {
			if ( path.size() == 1 ) {
				return true; // the root
			}
			if ( path.size() == 2 ) {
				if ( !Node_isEntity( path.top() ) ) {
					return false;
				}
				m_index.m_entities.emplace_back();
				m_index.m_entities.back().path = path;
				return true;
			}
			if ( path.size() == 3 && !m_index.m_entities.empty() && Node_isPrimitive( path.top() ) ) {
				m_index.m_entities.back().primitives.push_back( path );
			}
			return false; // nothing below a primitive is addressable
		}
	};

	bool build(){
		m_entities.clear();
		if ( !map_open() ) {
			return false;
		}
		m_revision = map_revision();
		GlobalSceneGraph().traverse( Walker( *this ) );
		return true;
	}

	static std::string entityId( std::size_t entity ){
		char buffer[32];
		snprintf( buffer, sizeof( buffer ), "e%lu", ( unsigned long )entity );
		return buffer;
	}
	static std::string primitiveId( std::size_t entity, std::size_t primitive ){
		char buffer[48];
		snprintf( buffer, sizeof( buffer ), "e%lu.p%lu", ( unsigned long )entity, ( unsigned long )primitive );
		return buffer;
	}

	/// \brief Resolves "e<n>" or "e<n>.p<m>".
	/// \return the path, or 0 when the id is malformed or out of range.
	const scene::Path* resolve( const char* id, bool& isEntity ) const {
		if ( id == 0 || *id != 'e' ) {
			return 0;
		}
		char* end = 0;
		const unsigned long entity = strtoul( id + 1, &end, 10 );
		if ( end == id + 1 || entity >= m_entities.size() ) {
			return 0;
		}
		if ( *end == '\0' ) {
			isEntity = true;
			return &m_entities[entity].path;
		}
		if ( end[0] != '.' || end[1] != 'p' ) {
			return 0;
		}
		const char* begin = end + 2;
		const unsigned long primitive = strtoul( begin, &end, 10 );
		if ( end == begin || *end != '\0' || primitive >= m_entities[entity].primitives.size() ) {
			return 0;
		}
		isEntity = false;
		return &m_entities[entity].primitives[primitive];
	}
};

inline const char* primitive_type( scene::Node& node ){
	return Node_isPatch( node ) ? "patch" : "brush";
}


//  *******************
// ** the RPC methods **
//  *******************

/// JSON-RPC reserved codes, plus the editor-state codes this bridge adds.
enum
{
	e_invalidParams = -32602,
	e_internal      = -32603,
	e_noMap         = -32000,
	e_unknownId     = -32001,
	e_staleRevision = -32002,
	e_unsupported   = -32003,
};

/// \brief One in-flight request. Methods read \c params, write \c result, and
/// return false after calling fail().
struct Call
{
	const json::Value& params;
	json::Writer& result;
	int errorCode = e_internal;
	std::string errorMessage = "unspecified failure";

	Call( const json::Value& params, json::Writer& result ) : params( params ), result( result ){
	}

	bool fail( int code, const char* message ){
		errorCode = code;
		errorMessage = message;
		return false;
	}

	/// \brief Builds the index and checks the caller's optional "revision" pin.
	bool index( SceneIndex& index ){
		if ( !index.build() ) {
			return fail( e_noMap, "no map is open" );
		}
		const json::Value& pinned = params["revision"];
		if ( pinned.isNumber() && std::size_t( pinned.number() ) != index.m_revision ) {
			return fail( e_staleRevision, "scene revision has moved; re-query ids" );
		}
		return true;
	}

	/// \brief Reads a [x, y, z] parameter.
	bool vector3( const char* name, Vector3& out ){
		double values[3];
		if ( !params[name].numbers( values, 3 ) ) {
			fail( e_invalidParams, "expected an array of three numbers" );
			return false;
		}
		out = Vector3( float( values[0] ), float( values[1] ), float( values[2] ) );
		return true;
	}
};


bool method_scene_stats( Call& call ){
	if ( !map_open() ) {
		return call.fail( e_noMap, "no map is open" );
	}
	SceneIndex index;
	index.build();

	std::size_t brushes = 0, patches = 0;
	for ( const SceneIndex::Entity& entity : index.m_entities )
	{
		for ( const scene::Path& primitive : entity.primitives )
		{
			if ( Node_isPatch( primitive.top() ) ) {
				++patches;
			}
			else {
				++brushes;
			}
		}
	}

	std::size_t selectedBrushes = 0, selectedPatches = 0, selectedEntities = 0;
	GlobalSelectionSystem().countSelectedStuff( selectedBrushes, selectedPatches, selectedEntities );

	call.result.beginObject();
	call.result.key( "protocol" ).number( c_protocolVersion );
	call.result.key( "revision" ).integer( index.m_revision );
	call.result.key( "map" ).string( GlobalRadiant().getMapName() );
	call.result.key( "saved" ).boolean( map_saved() );
	call.result.key( "counts" ).beginObject();
	call.result.key( "entities" ).integer( index.m_entities.size() );
	call.result.key( "brushes" ).integer( brushes );
	call.result.key( "patches" ).integer( patches );
	call.result.endObject();
	call.result.key( "selected" ).beginObject();
	call.result.key( "entities" ).integer( selectedEntities );
	call.result.key( "brushes" ).integer( selectedBrushes );
	call.result.key( "patches" ).integer( selectedPatches );
	call.result.endObject();
	call.result.key( "bounds" );
	write_bounds( call.result, scene_bounds() );
	call.result.key( "grid" ).number( GlobalRadiant().getGridSize() );
	call.result.endObject();
	return true;
}


/// \brief Writes {"revision":n,"ids":[...]} for everything currently selected.
void write_selection_ids( json::Writer& writer, const SceneIndex& index ){
	writer.key( "revision" ).integer( index.m_revision );
	writer.key( "ids" ).beginArray();
	for ( std::size_t e = 0; e < index.m_entities.size(); ++e )
	{
		const SceneIndex::Entity& entity = index.m_entities[e];
		scene::Instance* instance = GlobalSceneGraph().find( entity.path );
		if ( instance != 0 && Instance_isSelected( *instance ) ) {
			writer.string( SceneIndex::entityId( e ).c_str() );
		}
		for ( std::size_t p = 0; p < entity.primitives.size(); ++p )
		{
			instance = GlobalSceneGraph().find( entity.primitives[p] );
			if ( instance != 0 && Instance_isSelected( *instance ) ) {
				writer.string( SceneIndex::primitiveId( e, p ).c_str() );
			}
		}
	}
	writer.endArray();
}

bool method_scene_select( Call& call ){
	SceneIndex index;
	if ( !call.index( index ) ) {
		return false;
	}

	const json::Value& params = call.params;
	if ( !params["add"].boolean() ) {
		GlobalSelectionSystem().setSelectedAll( false );
	}

	if ( params["all"].boolean() ) {
		GlobalSelectionSystem().setSelectedAll( true );
	}
	else if ( params["ids"].isArray() ) {
		const json::Value& ids = params["ids"];
		for ( std::size_t i = 0; i < ids.size(); ++i )
		{
			bool isEntity = false;
			const scene::Path* path = index.resolve( ids.element( i ).string(), isEntity );
			if ( path == 0 ) {
				return call.fail( e_unknownId, "no such id" );
			}
			scene::Instance* instance = GlobalSceneGraph().find( *path );
			if ( instance == 0 ) {
				return call.fail( e_unknownId, "id is not instanced" );
			}
			// group entities own no selectable of their own; Entity_setSelected
			// selects their primitives instead, which is what the editor does.
			if ( isEntity ) {
				Entity_setSelected( *instance, true );
			}
			else {
				Instance_setSelected( *instance, true );
			}
		}
	}
	else if ( params["classname"].isString() || params["key"].isString() ) {
		const char* classname = params["classname"].string( 0 );
		const char* key = params["key"].string( 0 );
		const char* value = params["value"].string( 0 );
		if ( key != 0 && value == 0 ) {
			return call.fail( e_invalidParams, "\"key\" needs \"value\"" );
		}
		for ( const SceneIndex::Entity& item : index.m_entities )
		{
			Entity* entity = Node_getEntity( item.path.top() );
			if ( entity == 0
			  || ( classname != 0 && !string_equal( classname, entity->getClassName() ) )
			  || ( key != 0 && !string_equal( value, entity->getKeyValue( key ) ) ) ) {
				continue;
			}
			if ( scene::Instance* instance = GlobalSceneGraph().find( item.path ) ) {
				Entity_setSelected( *instance, true );
			}
		}
	}
	else if ( !params["none"].boolean() ) {
		return call.fail( e_invalidParams, "expected one of \"ids\", \"classname\", \"key\", \"all\", \"none\"" );
	}

	SceneChangeNotify();
	call.result.beginObject();
	write_selection_ids( call.result, index );
	call.result.endObject();
	return true;
}


class EntityKeyWriter : public Entity::Visitor
{
	json::Writer& m_writer;
public:
	EntityKeyWriter( json::Writer& writer ) : m_writer( writer ){
	}
	void visit( const char* key, const char* value ) override {
		m_writer.key( key ).string( value );
	}
};

bool method_scene_selection( Call& call ){
	SceneIndex index;
	if ( !call.index( index ) ) {
		return false;
	}

	call.result.beginObject();
	call.result.key( "revision" ).integer( index.m_revision );
	call.result.key( "bounds" );
	write_bounds( call.result, GlobalSelectionSystem().getBoundsSelected() );
	call.result.key( "items" ).beginArray();
	for ( std::size_t e = 0; e < index.m_entities.size(); ++e )
	{
		const SceneIndex::Entity& item = index.m_entities[e];
		scene::Instance* instance = GlobalSceneGraph().find( item.path );
		if ( instance != 0 && Instance_isSelected( *instance ) ) {
			Entity* entity = Node_getEntity( item.path.top() );
			call.result.beginObject();
			call.result.key( "id" ).string( SceneIndex::entityId( e ).c_str() );
			call.result.key( "type" ).string( "entity" );
			call.result.key( "classname" ).string( entity != 0 ? entity->getClassName() : "" );
			call.result.key( "bounds" );
			write_bounds( call.result, instance->worldAABB() );
			call.result.key( "keys" ).beginObject();
			if ( entity != 0 ) {
				EntityKeyWriter visitor( call.result );
				entity->forEachKeyValue( visitor );
			}
			call.result.endObject();
			call.result.endObject();
		}
		for ( std::size_t p = 0; p < item.primitives.size(); ++p )
		{
			instance = GlobalSceneGraph().find( item.primitives[p] );
			if ( instance == 0 || !Instance_isSelected( *instance ) ) {
				continue;
			}
			call.result.beginObject();
			call.result.key( "id" ).string( SceneIndex::primitiveId( e, p ).c_str() );
			call.result.key( "type" ).string( primitive_type( item.primitives[p].top() ) );
			call.result.key( "entity" ).string( SceneIndex::entityId( e ).c_str() );
			call.result.key( "bounds" );
			write_bounds( call.result, instance->worldAABB() );
			call.result.endObject();
		}
	}
	call.result.endArray();
	call.result.endObject();
	return true;
}


bool method_scene_transform( Call& call ){
	if ( !map_open() ) {
		return call.fail( e_noMap, "no map is open" );
	}
	if ( GlobalSelectionSystem().countSelected() == 0 ) {
		return call.fail( e_invalidParams, "nothing is selected" );
	}

	const json::Value& params = call.params;
	const bool rotate = params.has( "rotate" );
	const bool scale = params.has( "scale" );
	const bool translate = params.has( "translate" );
	if ( !rotate && !scale && !translate ) {
		return call.fail( e_invalidParams, "expected \"translate\", \"rotate\" or \"scale\"" );
	}

	// SelectionSystem exposes translate/rotate/scale, not an arbitrary matrix,
	// so the parameters are the three components rather than 16 numbers.
	// Applied rotate, then scale, then translate, about the selection pivot.
	Vector3 euler( 0, 0, 0 ), factor( 1, 1, 1 ), offset( 0, 0, 0 );
	if ( ( rotate && !call.vector3( "rotate", euler ) )
	  || ( scale && !call.vector3( "scale", factor ) )
	  || ( translate && !call.vector3( "translate", offset ) ) ) {
		return false;
	}
	if ( scale && ( factor.x() == 0 || factor.y() == 0 || factor.z() == 0 ) ) {
		return call.fail( e_invalidParams, "scale components must be non-zero" );
	}

	if ( rotate ) {
		GlobalSelectionSystem().rotateSelected( quaternion_for_matrix4_rotation( matrix4_rotation_for_euler_xyz_degrees( euler ) ) );
	}
	if ( scale ) {
		GlobalSelectionSystem().scaleSelected( factor );
	}
	if ( translate ) {
		GlobalSelectionSystem().translateSelected( offset );
	}
	SceneChangeNotify();

	call.result.beginObject();
	call.result.key( "revision" ).integer( map_revision() );
	call.result.key( "bounds" );
	write_bounds( call.result, GlobalSelectionSystem().getBoundsSelected() );
	call.result.endObject();
	return true;
}


bool method_scene_create_brush( Call& call ){
	if ( !map_open() ) {
		return call.fail( e_noMap, "no map is open" );
	}
	const json::Value& planes = call.params["planes"];
	if ( !planes.isArray() || planes.size() < 4 ) {
		return call.fail( e_invalidParams, "\"planes\" must be an array of at least four faces" );
	}

	// The default is whatever the texture browser has, matching what the editor
	// would do for a manually drawn brush.
	const char* fallbackShader = GlobalRadiant().TextureBrowser_getSelectedShader();

	NodeSmartReference node( GlobalBrushCreator().createBrush() );
	for ( std::size_t i = 0; i < planes.size(); ++i )
	{
		const json::Value& plane = planes.element( i );
		const json::Value& points = plane["points"];
		double p[3][3];
		if ( !points.isArray() || points.size() != 3
		  || !points.element( 0 ).numbers( p[0], 3 )
		  || !points.element( 1 ).numbers( p[1], 3 )
		  || !points.element( 2 ).numbers( p[2], 3 ) ) {
			return call.fail( e_invalidParams, "each face needs \"points\": three [x,y,z] triples" );
		}

		// Shader names include the "textures/" prefix here; the .map writer is
		// what strips it.
		const std::string shader = plane["shader"].string( fallbackShader );

		_QERFaceData face;
		face.m_p0 = DoubleVector3( p[0][0], p[0][1], p[0][2] );
		face.m_p1 = DoubleVector3( p[1][0], p[1][1], p[1][2] );
		face.m_p2 = DoubleVector3( p[2][0], p[2][1], p[2][2] );
		face.m_shader = shader.c_str();
		face.contents = int( plane["contents"].number( 0 ) );
		face.flags = int( plane["flags"].number( 0 ) );
		face.value = int( plane["value"].number( 0 ) );

		const json::Value& texdef = plane["texdef"];
		double pair[2];
		if ( texdef["shift"].numbers( pair, 2 ) ) {
			face.m_texdef.shift[0] = float( pair[0] );
			face.m_texdef.shift[1] = float( pair[1] );
		}
		if ( texdef["scale"].numbers( pair, 2 ) ) {
			face.m_texdef.scale[0] = float( pair[0] );
			face.m_texdef.scale[1] = float( pair[1] );
		}
		face.m_texdef.rotate = float( texdef["rotate"].number( 0 ) );

		if ( !GlobalBrushCreator().Brush_addFace( node, face ) ) {
			return call.fail( e_invalidParams, "face was rejected by the brush; check winding order" );
		}
	}

	Node_getTraversable( GlobalRadiant().getMapWorldEntity() )->insert( node );
	SceneChangeNotify();

	// Re-index so the caller gets the id of what it just made.
	SceneIndex index;
	index.build();
	std::string id;
	for ( std::size_t e = 0; e < index.m_entities.size() && id.empty(); ++e )
	{
		const std::vector<scene::Path>& primitives = index.m_entities[e].primitives;
		for ( std::size_t p = 0; p < primitives.size(); ++p )
		{
			if ( &primitives[p].top().get() == node.get_pointer() ) {
				id = SceneIndex::primitiveId( e, p );
				break;
			}
		}
	}

	call.result.beginObject();
	call.result.key( "revision" ).integer( index.m_revision );
	call.result.key( "id" ).string( id.c_str() );
	call.result.endObject();
	return true;
}


bool method_scene_create_entity( Call& call ){
	if ( !map_open() ) {
		return call.fail( e_noMap, "no map is open" );
	}
	const char* classname = call.params["classname"].string( 0 );
	if ( classname == 0 || string_empty( classname ) ) {
		return call.fail( e_invalidParams, "\"classname\" is required" );
	}
	if ( string_equal( classname, "worldspawn" ) ) {
		return call.fail( e_invalidParams, "worldspawn already exists and is not created this way" );
	}
	Vector3 origin( 0, 0, 0 );
	if ( call.params.has( "origin" ) && !call.vector3( "origin", origin ) ) {
		return false;
	}

	EntityClass* entityClass = GlobalEntityClassManager().findOrInsert( classname, false );
	if ( entityClass == 0 ) {
		return call.fail( e_invalidParams, "unknown entity class" );
	}
	// A group entity is only meaningful once it owns primitives, and reparenting
	// them needs Scene_parentSelectedBrushesToEntity, which lives in the core.
	// Refuse rather than create something the editor will discard on save.
	if ( !entityClass->fixedsize ) {
		return call.fail( e_unsupported, "only point (fixedsize) entity classes can be created through the bridge" );
	}

	NodeSmartReference node( GlobalEntityCreator().createEntity( entityClass ) );
	Node_getTraversable( GlobalSceneGraph().root() )->insert( node );

	scene::Path path( makeReference( GlobalSceneGraph().root() ) );
	path.push( makeReference( node.get() ) );
	scene::Instance* instance = GlobalSceneGraph().find( path );
	if ( instance == 0 ) {
		return call.fail( e_internal, "created entity was not instanced" );
	}

	// Point entities carry their position in the transform, not in the key; the
	// editor writes "origin" from the frozen transform. Same order as
	// Entity_createFromSelection().
	if ( Transformable* transform = Instance_getTransformable( *instance ) ) {
		transform->setType( TRANSFORM_PRIMITIVE );
		transform->setTranslation( origin );
		transform->freezeTransform();
	}

	Entity* entity = Node_getEntity( node );
	const json::Value& keys = call.params["keys"];
	if ( entity != 0 && keys.isObject() ) {
		for ( std::size_t i = 0; i < keys.size(); ++i )
		{
			if ( !string_equal( keys.key( i ), "classname" ) ) {
				entity->setKeyValue( keys.key( i ), keys.element( i ).string() );
			}
		}
	}
	SceneChangeNotify();

	SceneIndex index;
	index.build();
	std::string id;
	for ( std::size_t e = 0; e < index.m_entities.size(); ++e )
	{
		if ( &index.m_entities[e].path.top().get() == node.get_pointer() ) {
			id = SceneIndex::entityId( e );
			break;
		}
	}

	call.result.beginObject();
	call.result.key( "revision" ).integer( index.m_revision );
	call.result.key( "id" ).string( id.c_str() );
	call.result.endObject();
	return true;
}


bool method_scene_set_keys( Call& call ){
	SceneIndex index;
	if ( !call.index( index ) ) {
		return false;
	}
	bool isEntity = false;
	const scene::Path* path = index.resolve( call.params["id"].string(), isEntity );
	if ( path == 0 || !isEntity ) {
		return call.fail( e_unknownId, "\"id\" must name an entity" );
	}
	Entity* entity = Node_getEntity( path->top() );
	if ( entity == 0 ) {
		return call.fail( e_unknownId, "id does not name an entity" );
	}
	const json::Value& keys = call.params["keys"];
	if ( !keys.isObject() ) {
		return call.fail( e_invalidParams, "\"keys\" must be an object" );
	}

	for ( std::size_t i = 0; i < keys.size(); ++i )
	{
		const char* key = keys.key( i );
		if ( string_empty( key ) ) {
			return call.fail( e_invalidParams, "empty key" );
		}
		// Changing classname means replacing the node, not editing a key; the
		// editor has Scene_EntitySetClassname_Selected for that and ientity.h's
		// own EntityCopyingVisitor skips the key for the same reason.
		if ( string_equal( key, "classname" ) ) {
			return call.fail( e_unsupported, "classname cannot be changed through the bridge" );
		}
		const json::Value& value = keys.element( i );
		if ( value.isNull() ) {
			entity->setKeyValue( key, "" ); // an empty value erases the key
		}
		else if ( value.isString() ) {
			entity->setKeyValue( key, value.string() );
		}
		else if ( value.isNumber() ) {
			json::Writer number;
			number.number( value.number() );
			entity->setKeyValue( key, number.str().c_str() );
		}
		else if ( value.isBoolean() ) {
			entity->setKeyValue( key, value.boolean() ? "1" : "0" );
		}
		else {
			return call.fail( e_invalidParams, "key values must be string, number, boolean or null" );
		}
	}
	SceneChangeNotify();

	call.result.beginObject();
	call.result.key( "revision" ).integer( map_revision() );
	call.result.key( "keys" ).beginObject();
	EntityKeyWriter visitor( call.result );
	entity->forEachKeyValue( visitor );
	call.result.endObject();
	call.result.endObject();
	return true;
}


bool method_scene_delete( Call& call ){
	SceneIndex index;
	if ( !call.index( index ) ) {
		return false;
	}
	const json::Value& ids = call.params["ids"];
	if ( !ids.isArray() ) {
		return call.fail( e_invalidParams, "\"ids\" must be an array" );
	}

	// Resolve everything first: erasing a node invalidates nothing else in the
	// index, but erasing an entity does invalidate the paths of its primitives,
	// so entities are collected separately and their primitives dropped.
	std::vector<const scene::Path*> entities, primitives;
	for ( std::size_t i = 0; i < ids.size(); ++i )
	{
		bool isEntity = false;
		const scene::Path* path = index.resolve( ids.element( i ).string(), isEntity );
		if ( path == 0 ) {
			return call.fail( e_unknownId, "no such id" );
		}
		( isEntity ? entities : primitives ).push_back( path );
	}

	std::size_t deleted = 0;
	for ( const scene::Path* path : primitives )
	{
		bool owned = false;
		for ( const scene::Path* entity : entities )
		{
			owned = owned || &entity->top().get() == &path->parent().get();
		}
		if ( !owned ) {
			Path_deleteTop( *path );
			++deleted;
		}
	}
	for ( const scene::Path* path : entities )
	{
		Path_deleteTop( *path );
		++deleted;
	}
	SceneChangeNotify();

	call.result.beginObject();
	call.result.key( "revision" ).integer( map_revision() );
	call.result.key( "deleted" ).integer( deleted );
	call.result.endObject();
	return true;
}


bool method_camera_get( Call& call ){
	Vector3 origin( 0, 0, 0 ), angles( 0, 0, 0 );
	GlobalCamera().m_pfnGetCamera( origin, angles );
	call.result.beginObject();
	call.result.key( "origin" ).vector3( origin.data() );
	call.result.key( "angles" ).vector3( angles.data() );
	call.result.endObject();
	return true;
}

bool method_camera_set( Call& call ){
	Vector3 origin( 0, 0, 0 ), angles( 0, 0, 0 );
	GlobalCamera().m_pfnGetCamera( origin, angles );
	if ( call.params.has( "origin" ) && !call.vector3( "origin", origin ) ) {
		return false;
	}
	if ( call.params.has( "angles" ) && !call.vector3( "angles", angles ) ) {
		return false;
	}
	if ( call.params.has( "fov" ) ) {
		// _QERCameraTable carries origin and angles only, and CameraView::
		// setFieldOfView needs a CameraView a plugin cannot obtain.
		return call.fail( e_unsupported, "fov cannot be set through the bridge" );
	}
	GlobalCamera().m_pfnSetCamera( origin, angles );
	call.result.beginObject();
	call.result.key( "origin" ).vector3( origin.data() );
	call.result.key( "angles" ).vector3( angles.data() );
	call.result.endObject();
	return true;
}


bool method_map_path( Call& call ){
	const char* name = GlobalRadiant().getMapName();
	call.result.beginObject();
	call.result.key( "path" ).string( name );
	// Same test the core uses in Map_Unnamed().
	call.result.key( "unnamed" ).boolean( string_equal( name, "unnamed.map" ) );
	call.result.key( "saved" ).boolean( map_saved() );
	call.result.key( "revision" ).integer( map_revision() );
	call.result.key( "maps_path" ).string( GlobalRadiant().getMapsPath() );
	call.result.endObject();
	return true;
}

bool method_map_save( Call& call ){
	if ( !map_open() ) {
		return call.fail( e_noMap, "no map is open" );
	}
	const std::string name = GlobalRadiant().getMapName();
	if ( name.empty() || string_equal( name.c_str(), "unnamed.map" ) ) {
		return call.fail( e_noMap, "the map has no path; save it once from the editor first" );
	}

	// The core's Map_Save() is SaveReferences(), which saves every captured
	// resource. The current map is captured under its own name, so capturing it
	// again hands back the same resource with the refcount raised; Resource::
	// save() writes it and marks the change tracker saved, which fires
	// MapChanged() and updates the title. No core entry point needed.
	Resource* resource = GlobalReferenceCache().capture( name.c_str() );
	const bool written = resource != 0 && resource->save();
	GlobalReferenceCache().release( name.c_str() );

	call.result.beginObject();
	call.result.key( "path" ).string( name.c_str() );
	call.result.key( "written" ).boolean( written );
	call.result.key( "saved" ).boolean( map_saved() );
	call.result.key( "revision" ).integer( map_revision() );
	call.result.endObject();
	return true;
}

bool method_map_reload( Call& call ){
	if ( !map_open() ) {
		return call.fail( e_noMap, "no map is open" );
	}
	// This throws away unsaved editor state and clears the undo stack, so it is
	// not undoable and the caller has to say so out loud.
	if ( !call.params["discard_unsaved"].boolean() ) {
		return call.fail( e_invalidParams, "pass \"discard_unsaved\": true; a reload cannot be undone" );
	}
	const std::string name = GlobalRadiant().getMapName();
	if ( name.empty() || string_equal( name.c_str(), "unnamed.map" ) ) {
		return call.fail( e_noMap, "the map has no path to reload from" );
	}

	// Resource::refresh() reloads only when the file on disk has moved on, which
	// is exactly the case that matters: an external tool rewrote the .map.
	scene::Node* before = &GlobalSceneGraph().root();
	Resource* resource = GlobalReferenceCache().capture( name.c_str() );
	if ( resource != 0 ) {
		resource->refresh();
	}
	GlobalReferenceCache().release( name.c_str() );
	const bool reloaded = map_open() && &GlobalSceneGraph().root() != before;
	SceneChangeNotify();

	call.result.beginObject();
	call.result.key( "path" ).string( name.c_str() );
	call.result.key( "reloaded" ).boolean( reloaded );
	call.result.key( "revision" ).integer( map_revision() );
	call.result.endObject();
	return true;
}


bool method_undo_undo( Call& call ){
	if ( !map_open() ) {
		return call.fail( e_noMap, "no map is open" );
	}
	// Exactly what the core's Undo() does.
	GlobalUndoSystem().undo();
	SceneChangeNotify();
	call.result.beginObject();
	call.result.key( "revision" ).integer( map_revision() );
	call.result.key( "depth" ).integer( GlobalUndoSystem().size() );
	call.result.endObject();
	return true;
}


/// \brief The whole RPC surface. \c mutating decides whether the dispatcher
/// opens an UndoableCommand; \c calls is the usage count that decides whether a
/// method survives to the pull request.
struct Method
{
	const char* name;
	bool mutating;
	bool ( *invoke )( Call& call );
	std::size_t calls;
};

Method g_methods[] = {
	{ "scene.stats",          false, method_scene_stats,         0 },
	{ "scene.select",         false, method_scene_select,        0 },
	{ "scene.selection",      false, method_scene_selection,     0 },
	{ "scene.transform",      true,  method_scene_transform,     0 },
	{ "scene.create_brush",   true,  method_scene_create_brush,  0 },
	{ "scene.create_entity",  true,  method_scene_create_entity, 0 },
	{ "scene.set_keys",       true,  method_scene_set_keys,      0 },
	{ "scene.delete",         true,  method_scene_delete,        0 },
	{ "camera.get",           false, method_camera_get,          0 },
	{ "camera.set",           false, method_camera_set,          0 },
	{ "map.path",             false, method_map_path,            0 },
	{ "map.save",             false, method_map_save,            0 },
	{ "map.reload",           false, method_map_reload,          0 },
	{ "undo.undo",            false, method_undo_undo,           0 },
};

Method* find_method( const char* name ){
	for ( Method& method : g_methods )
	{
		if ( string_equal( method.name, name ) ) {
			return &method;
		}
	}
	return 0;
}

void reportUsage(){
	globalOutputStream() << "MCPBridge: RPC usage this session\n";
	for ( const Method& method : g_methods )
	{
		globalOutputStream() << "  " << method.name << ": " << method.calls << '\n';
	}
}


//  ****************
// ** JSON-RPC 2.0 **
//  ****************

void write_id( json::Writer& writer, const json::Value& id ){
	if ( id.isNumber() ) {
		writer.number( id.number() );
	}
	else if ( id.isString() ) {
		writer.string( id.string() );
	}
	else {
		writer.null();
	}
}

void write_error( json::Writer& writer, const json::Value& id, int code, const char* message ){
	writer.beginObject();
	writer.key( "jsonrpc" ).string( "2.0" );
	writer.key( "id" );
	write_id( writer, id );
	writer.key( "error" ).beginObject();
	writer.key( "code" ).number( code );
	writer.key( "message" ).string( message );
	writer.endObject();
	writer.endObject();
}

/// \brief Runs one request object. Appends a response unless it is a notification.
///
/// A request with no "id" member and one with "id": null are treated alike: both
/// are notifications and get no reply. The JSON-RPC spec allows a null id on a
/// real request but advises against it, and telling them apart would be the only
/// reason for the Value type to distinguish absent from null.
void dispatch_request( const json::Value& request, json::Writer& out, bool& wroteAny ){
	const json::Value& id = request["id"];
	const bool notification = id.isNull();

	// An invalid request always gets an answer, even inside a batch: there is no
	// way to know whether the sender meant it as a notification.
	if ( !request.isObject() || !string_equal( request["jsonrpc"].string(), "2.0" ) || !request["method"].isString() ) {
		write_error( out, id, -32600, "invalid request" );
		wroteAny = true;
		return;
	}

	const char* name = request["method"].string();
	Method* method = find_method( name );
	if ( method == 0 ) {
		if ( !notification ) {
			write_error( out, id, -32601, "method not found" );
			wroteAny = true;
		}
		return;
	}

	++method->calls;
	if ( settings().logCalls ) {
		globalOutputStream() << "MCPBridge: " << name << '\n';
	}

	json::Writer result;
	Call call( request["params"], result );
	const bool ok = method->invoke( call );

	if ( notification ) {
		return;
	}
	if ( ok ) {
		out.beginObject();
		out.key( "jsonrpc" ).string( "2.0" );
		out.key( "id" );
		write_id( out, id );
		out.key( "result" ).raw( result.str() );
		out.endObject();
	}
	else {
		write_error( out, id, call.errorCode, call.errorMessage.c_str() );
	}
	wroteAny = true;
}

/// \brief Runs one received line: a request object or a batch array.
/// \return the response line, or "" when everything in it was a notification.
///
/// The whole line is wrapped in a single UndoableCommand if any method in it
/// mutates. That is deliberately how a caller groups work into one undo step:
/// a JSON-RPC batch is one Ctrl+Z. There is no undo.begin/undo.end pair,
/// because a pair that straddles requests leaves the editor's undo system
/// half-open if the client disconnects mid-operation.
std::string dispatch_line( const char* line ){
	json::Writer out;
	json::Value document;

	if ( !json::parse( line, document ) ) {
		write_error( out, json::value_null(), -32700, "parse error" );
		return out.str();
	}

	const bool batch = document.isArray();
	if ( batch && document.size() == 0 ) {
		write_error( out, json::value_null(), -32600, "empty batch" );
		return out.str();
	}

	// Decide the undo label before running anything, so the editor's undo menu
	// names the agent's operation rather than the last primitive it touched.
	std::string label;
	for ( std::size_t i = 0; i < ( batch ? document.size() : 1 ); ++i )
	{
		const json::Value& request = batch ? document.element( i ) : document;
		const Method* method = find_method( request["method"].string() );
		if ( method != 0 && method->mutating ) {
			if ( label.empty() ) {
				label = "MCPBridge.";
				label += method->name;
			}
			else {
				label = "MCPBridge.batch";
				break;
			}
		}
	}
	std::optional<UndoableCommand> undo;
	if ( !label.empty() ) {
		undo.emplace( label.c_str() );
	}

	bool wroteAny = false;
	if ( batch ) {
		out.beginArray();
		for ( std::size_t i = 0; i < document.size(); ++i )
		{
			dispatch_request( document.element( i ), out, wroteAny );
		}
		out.endArray();
		return wroteAny ? out.str() : std::string();
	}
	dispatch_request( document, out, wroteAny );
	return wroteAny ? out.str() : std::string();
}


//  ***********************
// ** the loopback socket **
//  ***********************

#ifdef WIN32
typedef SOCKET socket_t;
const socket_t c_invalidSocket = INVALID_SOCKET;
inline void socket_close( socket_t s ){
	closesocket( s );
}
inline int socket_error(){
	return WSAGetLastError();
}
inline void socket_nonblocking( socket_t s ){
	u_long mode = 1;
	ioctlsocket( s, FIONBIO, &mode );
}
#else
typedef int socket_t;
const socket_t c_invalidSocket = -1;
inline void socket_close( socket_t s ){
	close( s );
}
inline int socket_error(){
	return errno;
}
inline void socket_nonblocking( socket_t s ){
	fcntl( s, F_SETFL, fcntl( s, F_GETFL, 0 ) | O_NONBLOCK );
}
#endif

const std::size_t c_maxLine = 1024 * 1024;

/// \brief One accepted connection. Sockets stay blocking with short timeouts:
/// a read only happens after QSocketNotifier says data is there, and a write to
/// a loopback peer either completes or times out. That is a lot less code than
/// an outgoing-buffer state machine, and the cost is bounded by the timeout.
struct Connection
{
	socket_t socket = c_invalidSocket;
	QSocketNotifier* notifier = 0;
	std::string incoming;
	bool authenticated = false;
};

std::vector<Connection*> g_connections;
socket_t g_listener = c_invalidSocket;
QSocketNotifier* g_listenNotifier = 0;
std::string g_secret;
#ifdef WIN32
bool g_winsock = false;
#endif

/// Re-entrancy guard. ScreenUpdates_Disable() pumps the Qt event loop during
/// map load and save, so a notifier can fire in the middle of an operation.
/// Nothing here is re-entrant, so a second request waits with its notifier off.
bool g_dispatching = false;
std::vector<QSocketNotifier*> g_deferred;

void socket_configure( socket_t s ){
	int one = 1;
	setsockopt( s, IPPROTO_TCP, TCP_NODELAY, ( const char* )&one, sizeof( one ) );
	// Bounded blocking: a peer that stops reading costs five seconds, not the
	// editor's event loop.
#ifdef WIN32
	DWORD timeout = 5000;
	setsockopt( s, SOL_SOCKET, SO_RCVTIMEO, ( const char* )&timeout, sizeof( timeout ) );
	setsockopt( s, SOL_SOCKET, SO_SNDTIMEO, ( const char* )&timeout, sizeof( timeout ) );
#else
	struct timeval timeout;
	timeout.tv_sec = 5;
	timeout.tv_usec = 0;
	setsockopt( s, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof( timeout ) );
	setsockopt( s, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof( timeout ) );
#endif
}

/// \brief Compares the handshake in time that does not depend on where the
/// first wrong byte is.
bool secret_equal( const std::string& offered, const std::string& expected ){
	if ( offered.size() != expected.size() ) {
		return false;
	}
	unsigned char difference = 0;
	for ( std::size_t i = 0; i < expected.size(); ++i )
	{
		difference |= ( unsigned char )( offered[i] ^ expected[i] );
	}
	return difference == 0;
}

void connection_close( Connection* connection ){
	for ( std::size_t i = 0; i < g_connections.size(); ++i )
	{
		if ( g_connections[i] == connection ) {
			g_connections.erase( g_connections.begin() + i );
			break;
		}
	}
	delete connection->notifier;
	if ( connection->socket != c_invalidSocket ) {
		socket_close( connection->socket );
	}
	delete connection;
}

bool connection_send( Connection* connection, const std::string& line ){
	std::string payload = line;
	payload += '\n';
	std::size_t sent = 0;
	while ( sent < payload.size() )
	{
		const int written = int( send( connection->socket, payload.c_str() + sent, int( payload.size() - sent ), 0 ) );
		if ( written <= 0 ) {
			return false;
		}
		sent += std::size_t( written );
	}
	return true;
}

/// \brief Handles one complete line. \return false to drop the connection.
bool connection_line( Connection* connection, const std::string& line ){
	if ( !connection->authenticated ) {
		// The handshake is a bare line rather than an RPC method, so that the
		// surface stays at what §9.2 lists and an unauthenticated peer never
		// reaches the dispatcher at all.
		if ( !secret_equal( line, g_secret ) ) {
			globalWarningStream() << "MCPBridge: rejected a connection with a bad secret\n";
			return false;
		}
		connection->authenticated = true;
		return connection_send( connection, "{\"jsonrpc\":\"2.0\",\"id\":null,\"result\":{\"ready\":true}}" );
	}
	if ( line.empty() ) {
		return true; // keep-alive
	}
	const std::string response = dispatch_line( line.c_str() );
	return response.empty() || connection_send( connection, response );
}

void connection_read( Connection* connection ){
	char buffer[8192];
	const int count = int( recv( connection->socket, buffer, int( sizeof( buffer ) ), 0 ) );
	if ( count <= 0 ) {
		connection_close( connection );
		return;
	}
	connection->incoming.append( buffer, std::size_t( count ) );

	for (;; )
	{
		const std::size_t newline = connection->incoming.find( '\n' );
		if ( newline == std::string::npos ) {
			if ( connection->incoming.size() > c_maxLine ) {
				globalErrorStream() << "MCPBridge: dropping a connection that sent an over-long line\n";
				connection_close( connection );
			}
			return;
		}
		std::string line = connection->incoming.substr( 0, newline );
		connection->incoming.erase( 0, newline + 1 );
		if ( !line.empty() && line[line.size() - 1] == '\r' ) {
			line.erase( line.size() - 1 );
		}
		if ( !connection_line( connection, line ) ) {
			connection_close( connection );
			return;
		}
	}
}

/// \brief True when \p notifier is still one we own. Called after work() may
/// have closed a connection, so the pointer is compared, never dereferenced.
bool notifier_alive( QSocketNotifier* notifier ){
	if ( notifier != 0 && notifier == g_listenNotifier ) {
		return true;
	}
	for ( const Connection* connection : g_connections )
	{
		if ( connection->notifier == notifier ) {
			return true;
		}
	}
	return false;
}

/// \brief Runs \p work with the re-entrancy guard held.
template<typename Work>
void guarded( QSocketNotifier* notifier, const Work& work ){
	if ( g_dispatching ) {
		notifier->setEnabled( false );
		g_deferred.push_back( notifier );
		return;
	}
	g_dispatching = true;
	notifier->setEnabled( false );
	work();
	if ( notifier_alive( notifier ) ) {
		notifier->setEnabled( true );
	}
	g_dispatching = false;

	const std::vector<QSocketNotifier*> deferred = g_deferred;
	g_deferred.clear();
	for ( QSocketNotifier* pending : deferred )
	{
		if ( notifier_alive( pending ) ) {
			pending->setEnabled( true );
		}
	}
}

/// \brief Wires a notifier's read signal, and keeps our pointer honest if Qt
/// destroys it first (it is parented to the main window, which dies before the
/// module is released).
template<typename Handler>
void notifier_connect( QSocketNotifier* notifier, const Handler& handler ){
	QObject::connect( notifier,
	                  static_cast<void ( QSocketNotifier::* )( int )>( &QSocketNotifier::activated ),
	                  notifier,
	                  [handler]( int ){ handler(); } );
}

void listener_accept(){
	const socket_t accepted = accept( g_listener, 0, 0 );
	if ( accepted == c_invalidSocket ) {
		return; // spurious readiness, or the peer went away
	}
	if ( int( g_connections.size() ) >= settings().maxConnections ) {
		globalWarningStream() << "MCPBridge: refusing a connection, limit reached\n";
		socket_close( accepted );
		return;
	}
	socket_configure( accepted );

	Connection* connection = new Connection;
	connection->socket = accepted;
	connection->authenticated = g_secret.empty();
	connection->notifier = new QSocketNotifier( qintptr( accepted ), QSocketNotifier::Read, g_mainWindow );
	g_connections.push_back( connection );

	QObject::connect( connection->notifier, &QObject::destroyed,
	                  [connection](){ connection->notifier = 0; } );
	notifier_connect( connection->notifier, [connection](){
		guarded( connection->notifier, [connection](){ connection_read( connection ); } );
	} );
}

bool listening(){
	return g_listener != c_invalidSocket;
}

bool start(){
#ifndef MCPBRIDGE_ENABLED
	globalErrorStream() << "MCPBridge: built without MCPBRIDGE_ENABLED; the socket cannot be opened\n";
	return false;
#else
	if ( listening() ) {
		return true;
	}
	if ( !settings().enabled ) {
		globalErrorStream() << "MCPBridge: disabled by preference; nothing was opened\n";
		return false;
	}
#ifdef WIN32
	WSADATA wsadata;
	if ( WSAStartup( MAKEWORD( 2, 2 ), &wsadata ) != 0 ) {
		globalErrorStream() << "MCPBridge: WSAStartup failed\n";
		return false;
	}
	g_winsock = true;
#endif
	const char* secret = getenv( "NRC_MCPBRIDGE_SECRET" );
	g_secret = secret != 0 ? secret : "";
	if ( g_secret.empty() ) {
		globalWarningStream() << "MCPBridge: NRC_MCPBRIDGE_SECRET is unset - any local process can drive this editor\n";
	}

	g_listener = socket( AF_INET, SOCK_STREAM, IPPROTO_TCP );
	if ( g_listener == c_invalidSocket ) {
		globalErrorStream() << "MCPBridge: socket() failed, error " << socket_error() << '\n';
		return false;
	}

	// Loopback only, and never SO_REUSEADDR: a stale socket on this port must
	// look like a failure, not like something to take over.
	struct sockaddr_in address;
	memset( &address, 0, sizeof( address ) );
	address.sin_family = AF_INET;
	address.sin_addr.s_addr = htonl( INADDR_LOOPBACK );
	address.sin_port = htons( ( unsigned short )settings().port );

	if ( bind( g_listener, ( const struct sockaddr* )&address, sizeof( address ) ) != 0
	  || listen( g_listener, 4 ) != 0 ) {
		globalErrorStream() << "MCPBridge: cannot listen on 127.0.0.1:" << settings().port << ", error " << socket_error() << '\n';
		socket_close( g_listener );
		g_listener = c_invalidSocket;
		return false;
	}
	// The listener is non-blocking so that a spurious readiness notification
	// cannot park the whole editor inside accept(). Accepted sockets stay
	// blocking - they do not inherit the flag - and rely on their timeouts.
	socket_nonblocking( g_listener );

	g_listenNotifier = new QSocketNotifier( qintptr( g_listener ), QSocketNotifier::Read, g_mainWindow );
	QObject::connect( g_listenNotifier, &QObject::destroyed, [](){ g_listenNotifier = 0; } );
	notifier_connect( g_listenNotifier, [](){
		guarded( g_listenNotifier, [](){ listener_accept(); } );
	} );

	globalOutputStream() << "MCPBridge: listening on 127.0.0.1:" << settings().port
	                     << ( g_secret.empty() ? " (no shared secret)\n" : " (shared secret required)\n" );
	return true;
#endif // MCPBRIDGE_ENABLED
}

void stop(){
	while ( !g_connections.empty() )
	{
		connection_close( g_connections.back() );
	}
	g_deferred.clear();
	delete g_listenNotifier;
	g_listenNotifier = 0;
	if ( g_listener != c_invalidSocket ) {
		socket_close( g_listener );
		g_listener = c_invalidSocket;
		globalOutputStream() << "MCPBridge: stopped listening\n";
	}
#ifdef WIN32
	if ( g_winsock ) {
		WSACleanup();
		g_winsock = false;
	}
#endif
}


//  ***********************
// ** standard plugin stuff **
//  ***********************

const char* init( void* hApp, void* pMainWidget ){
	g_mainWindow = static_cast<QWidget*>( pMainWidget );

	GlobalPreferenceSystem().registerPreference( "MCPBridge_Enabled",
	                                             BoolImportStringCaller( settings().enabled ),
	                                             BoolExportStringCaller( settings().enabled ) );
	GlobalPreferenceSystem().registerPreference( "MCPBridge_Port",
	                                             IntImportStringCaller( settings().port ),
	                                             IntExportStringCaller( settings().port ) );
	GlobalPreferenceSystem().registerPreference( "MCPBridge_MaxConnections",
	                                             IntImportStringCaller( settings().maxConnections ),
	                                             IntExportStringCaller( settings().maxConnections ) );
	GlobalPreferenceSystem().registerPreference( "MCPBridge_LogCalls",
	                                             BoolImportStringCaller( settings().logCalls ),
	                                             BoolExportStringCaller( settings().logCalls ) );
	if ( settings().port <= 0 || settings().port > 65535 ) {
		settings().port = Settings().port;
	}

	// init() runs before the event loop and before any map exists, so the
	// listener waits for the loop to start rather than opening here.
	if ( settings().enabled ) {
		QTimer::singleShot( 0, [](){ start(); } );
	}
	return "Initialising MCPBridge";
}

const char* getName(){
	return "MCP Bridge";
}

const char* getCommandList(){
	return "Start listening;Stop listening;-;Log RPC usage;About...";
}

const char* getCommandTitleList(){
	return "";
}

void about(){
	const auto text = StringStream<256>(
	    "MCP bridge ", c_protocolVersion, " - newline-delimited JSON-RPC 2.0 on 127.0.0.1\n\n",
	    listening() ? "Listening on port " : "Not listening. Configured port ", settings().port, "\n",
	    "Preference: MCPBridge_Enabled = ", settings().enabled ? "true" : "false", "\n\n",
	    "While this is listening, any process on this machine that can open a\n"
	    "loopback socket can read and modify the open map." );
	GlobalRadiant().m_pfnMessageBox( g_mainWindow, text, "MCP Bridge", EMessageBoxType::Info, 0 );
}

void dispatch( const char* command, float* vMin, float* vMax, bool bSingleBrush ){
	if ( string_equal( command, "Start listening" ) ) {
		// The preference is the toggle, so flipping it here is what makes the
		// choice persist to the next session.
		settings().enabled = true;
		if ( !start() ) {
			settings().enabled = false;
		}
	}
	else if ( string_equal( command, "Stop listening" ) ) {
		settings().enabled = false;
		stop();
	}
	else if ( string_equal( command, "Log RPC usage" ) ) {
		reportUsage();
	}
	else if ( string_equal( command, "About..." ) ) {
		about();
	}
}

} // namespace MCPBridge


class MCPBridgeDependencies :
	public GlobalRadiantModuleRef,      // the core API: map name, paths, message boxes
	public GlobalPreferenceSystemModuleRef, // the runtime on/off switch
	public GlobalUndoModuleRef,         // one UndoableCommand per mutating request
	public GlobalSceneGraphModuleRef,   // the scene to read and mutate
	public GlobalSelectionModuleRef,    // selection queries and transforms
	public GlobalReferenceModuleRef,    // map save and reload through the resource
	public GlobalCameraModuleRef,       // camera get/set
	public GlobalEntityModuleRef,       // entity creation
	public GlobalEntityClassManagerModuleRef,
	public GlobalBrushModuleRef        // brush creation
{
public:
	MCPBridgeDependencies() :
		GlobalEntityModuleRef( GlobalRadiant().getRequiredGameDescriptionKeyValue( "entities" ) ),
		GlobalEntityClassManagerModuleRef( GlobalRadiant().getRequiredGameDescriptionKeyValue( "entityclass" ) ),
		GlobalBrushModuleRef( GlobalRadiant().getRequiredGameDescriptionKeyValue( "brushtypes" ) ){
	}
	~MCPBridgeDependencies(){
		MCPBridge::stop();
	}
};

class MCPBridgeModule : public TypeSystemRef
{
	_QERPluginTable m_plugin;
public:
	typedef _QERPluginTable Type;
	STRING_CONSTANT( Name, "MCPBridge" );

	MCPBridgeModule(){
		m_plugin.m_pfnQERPlug_Init = &MCPBridge::init;
		m_plugin.m_pfnQERPlug_GetName = &MCPBridge::getName;
		m_plugin.m_pfnQERPlug_GetCommandList = &MCPBridge::getCommandList;
		m_plugin.m_pfnQERPlug_GetCommandTitleList = &MCPBridge::getCommandTitleList;
		m_plugin.m_pfnQERPlug_Dispatch = &MCPBridge::dispatch;
	}
	_QERPluginTable* getTable(){
		return &m_plugin;
	}
};

typedef SingletonModule<MCPBridgeModule, MCPBridgeDependencies> SingletonMCPBridgeModule;

SingletonMCPBridgeModule g_MCPBridgeModule;


extern "C" void RADIANT_DLLEXPORT Radiant_RegisterModules( ModuleServer& server ){
	initialiseModule( server );

	g_MCPBridgeModule.selfRegister();
}
