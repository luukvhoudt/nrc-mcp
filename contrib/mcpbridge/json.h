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

/// \file
/// \brief The smallest JSON reader/writer that newline-delimited JSON-RPC 2.0 needs.
///
/// Hand-rolled on purpose. The bridge is worth reviewing only if it adds no
/// third-party dependency, and the subset of JSON an RPC line uses is small
/// enough that a parser is cheaper than an argument about vendoring.
///
/// Radiant is built with -fno-exceptions and -fno-rtti, so nothing here throws
/// and nothing here casts dynamically: parsing reports failure by return value
/// and every accessor takes a fallback.

#pragma once

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace json
{

enum class Type
{
	Null,
	Boolean,
	Number,
	String,
	Array,
	Object,
};

/// \brief A parsed JSON value.
///
/// Every payload is stored inline instead of in a union. That wastes a few
/// hundred bytes per node and buys a type that cannot be read as the wrong
/// thing. RPC requests are a few hundred bytes; the trade is free here.
class Value
{
	Type m_type = Type::Null;
	bool m_boolean = false;
	double m_number = 0;
	std::string m_string;
	std::vector<std::string> m_keys;   // object member names, in document order
	std::vector<Value> m_values;       // array elements, or object member values

	friend class Reader;

public:
	Type type() const { return m_type; }
	bool isNull() const { return m_type == Type::Null; }
	bool isBoolean() const { return m_type == Type::Boolean; }
	bool isNumber() const { return m_type == Type::Number; }
	bool isString() const { return m_type == Type::String; }
	bool isArray() const { return m_type == Type::Array; }
	bool isObject() const { return m_type == Type::Object; }

	bool boolean( bool fallback = false ) const { return m_type == Type::Boolean ? m_boolean : fallback; }
	double number( double fallback = 0 ) const { return m_type == Type::Number ? m_number : fallback; }
	/// \brief Returns the string, or \p fallback for any other type. Never null.
	const char* string( const char* fallback = "" ) const { return m_type == Type::String ? m_string.c_str() : fallback; }

	/// \brief Number of array elements or object members; 0 for scalars.
	std::size_t size() const { return m_values.size(); }
	/// \brief Array element, or object member value, by position.
	/// Deliberately not an operator[] overload: a literal 0 is both an integer
	/// and a null pointer constant, so index-or-name overloading is ambiguous.
	const Value& element( std::size_t index ) const;
	/// \brief Object member by name. Missing members read as null.
	const Value& operator[]( const char* key ) const;
	/// \brief Name of the object member at \p index, or "" if there is none.
	const char* key( std::size_t index ) const { return index < m_keys.size() ? m_keys[index].c_str() : ""; }
	bool has( const char* key ) const { return !( *this )[key].isNull(); }

	/// \brief Reads \p count numbers from an array value into \p out.
	/// \return false unless the value is an array of exactly \p count numbers.
	bool numbers( double* out, std::size_t count ) const {
		if ( m_type != Type::Array || m_values.size() != count ) {
			return false;
		}
		for ( std::size_t i = 0; i < count; ++i )
		{
			if ( !m_values[i].isNumber() ) {
				return false;
			}
			out[i] = m_values[i].m_number;
		}
		return true;
	}
};

inline const Value& value_null(){
	static const Value nil;
	return nil;
}

inline const Value& Value::element( std::size_t index ) const {
	return index < m_values.size() ? m_values[index] : value_null();
}

inline const Value& Value::operator[]( const char* key ) const {
	for ( std::size_t i = 0; i < m_keys.size(); ++i )
	{
		if ( m_keys[i] == key ) {
			return m_values[i];
		}
	}
	return value_null();
}


/// \brief Recursive-descent reader over a NUL-terminated buffer.
class Reader
{
	const char* m_pos;
	std::size_t m_depth = 0;

	/// Hostile input must not be able to overflow the stack.
	static const std::size_t c_maxDepth = 32;

	void skipSpace(){
		while ( *m_pos == ' ' || *m_pos == '\t' || *m_pos == '\r' || *m_pos == '\n' )
			++m_pos;
	}
	bool literal( const char* text ){
		const std::size_t length = strlen( text );
		if ( strncmp( m_pos, text, length ) != 0 ) {
			return false;
		}
		m_pos += length;
		return true;
	}
	static void encodeUtf8( std::string& out, unsigned int code ){
		if ( code < 0x80 ) {
			out += char( code );
		}
		else if ( code < 0x800 ) {
			out += char( 0xC0 | ( code >> 6 ) );
			out += char( 0x80 | ( code & 0x3F ) );
		}
		else if ( code < 0x10000 ) {
			out += char( 0xE0 | ( code >> 12 ) );
			out += char( 0x80 | ( ( code >> 6 ) & 0x3F ) );
			out += char( 0x80 | ( code & 0x3F ) );
		}
		else {
			out += char( 0xF0 | ( code >> 18 ) );
			out += char( 0x80 | ( ( code >> 12 ) & 0x3F ) );
			out += char( 0x80 | ( ( code >> 6 ) & 0x3F ) );
			out += char( 0x80 | ( code & 0x3F ) );
		}
	}
	bool hex4( unsigned int& code ){
		code = 0;
		for ( int i = 0; i < 4; ++i )
		{
			const char c = *m_pos++;
			code <<= 4;
			if ( c >= '0' && c <= '9' ) {
				code |= unsigned( c - '0' );
			}
			else if ( c >= 'a' && c <= 'f' ) {
				code |= unsigned( c - 'a' + 10 );
			}
			else if ( c >= 'A' && c <= 'F' ) {
				code |= unsigned( c - 'A' + 10 );
			}
			else {
				return false;
			}
		}
		return true;
	}
	bool readString( std::string& out ){
		if ( *m_pos++ != '"' ) {
			return false;
		}
		for (;; )
		{
			const char c = *m_pos++;
			if ( c == '"' ) {
				return true;
			}
			if ( c == '\0' ) {
				return false; // unterminated
			}
			if ( c != '\\' ) {
				if ( ( unsigned char )c < 0x20 ) {
					return false; // raw control character
				}
				out += c;
				continue;
			}
			switch ( *m_pos++ )
			{
			case '"': out += '"'; break;
			case '\\': out += '\\'; break;
			case '/': out += '/'; break;
			case 'b': out += '\b'; break;
			case 'f': out += '\f'; break;
			case 'n': out += '\n'; break;
			case 'r': out += '\r'; break;
			case 't': out += '\t'; break;
			case 'u': {
				unsigned int code;
				if ( !hex4( code ) ) {
					return false;
				}
				if ( code >= 0xD800 && code < 0xDC00 && m_pos[0] == '\\' && m_pos[1] == 'u' ) {
					unsigned int low;
					m_pos += 2;
					if ( !hex4( low ) ) {
						return false;
					}
					code = 0x10000 + ( ( code - 0xD800 ) << 10 ) + ( low - 0xDC00 );
				}
				encodeUtf8( out, code );
				break;
			}
			default:
				return false;
			}
		}
	}

public:
	Reader( const char* text ) : m_pos( text ){
	}

	bool read( Value& value ){
		if ( ++m_depth > c_maxDepth ) {
			return false;
		}
		skipSpace();
		switch ( *m_pos )
		{
		case 'n':
			value.m_type = Type::Null;
			if ( !literal( "null" ) ) {
				return false;
			}
			break;
		case 't':
			value.m_type = Type::Boolean;
			value.m_boolean = true;
			if ( !literal( "true" ) ) {
				return false;
			}
			break;
		case 'f':
			value.m_type = Type::Boolean;
			value.m_boolean = false;
			if ( !literal( "false" ) ) {
				return false;
			}
			break;
		case '"':
			value.m_type = Type::String;
			if ( !readString( value.m_string ) ) {
				return false;
			}
			break;
		case '[':
			value.m_type = Type::Array;
			++m_pos;
			skipSpace();
			if ( *m_pos == ']' ) {
				++m_pos;
				break;
			}
			for (;; )
			{
				value.m_values.emplace_back();
				if ( !read( value.m_values.back() ) ) {
					return false;
				}
				skipSpace();
				if ( *m_pos == ',' ) {
					++m_pos;
					continue;
				}
				if ( *m_pos == ']' ) {
					++m_pos;
					break;
				}
				return false;
			}
			break;
		case '{':
			value.m_type = Type::Object;
			++m_pos;
			skipSpace();
			if ( *m_pos == '}' ) {
				++m_pos;
				break;
			}
			for (;; )
			{
				skipSpace();
				value.m_keys.emplace_back();
				if ( !readString( value.m_keys.back() ) ) {
					return false;
				}
				skipSpace();
				if ( *m_pos++ != ':' ) {
					return false;
				}
				value.m_values.emplace_back();
				if ( !read( value.m_values.back() ) ) {
					return false;
				}
				skipSpace();
				if ( *m_pos == ',' ) {
					++m_pos;
					continue;
				}
				if ( *m_pos == '}' ) {
					++m_pos;
					break;
				}
				return false;
			}
			break;
		default: {
			// strtod accepts a leading '+', hex, "inf" and leading zeroes; JSON
			// accepts none of those, so the shape is checked before converting.
			const char* begin = m_pos;
			if ( *m_pos == '-' ) {
				++m_pos;
			}
			if ( *m_pos < '0' || *m_pos > '9' ) {
				return false;
			}
			if ( m_pos[0] == '0' && m_pos[1] >= '0' && m_pos[1] <= '9' ) {
				return false;
			}
			char* end = 0;
			value.m_type = Type::Number;
			value.m_number = strtod( begin, &end );
			if ( end == begin || !std::isfinite( value.m_number ) ) {
				return false;
			}
			m_pos = end;
			break;
		}
		}
		--m_depth;
		return true;
	}

	/// \brief Reads one complete value and checks that only whitespace follows.
	bool readDocument( Value& value ){
		if ( !read( value ) ) {
			return false;
		}
		skipSpace();
		return *m_pos == '\0';
	}
};

/// \brief Parses one JSON document. \return false on any syntax error.
inline bool parse( const char* text, Value& value ){
	return Reader( text ).readDocument( value );
}


/// \brief Append-only JSON writer. Tracks separators so callers cannot emit
/// invalid punctuation, and formats numbers so that they round-trip.
class Writer
{
	std::string m_out;
	bool m_comma = false;

	void separate(){
		if ( m_comma ) {
			m_out += ',';
		}
		m_comma = true;
	}
	void open( char bracket ){
		separate();
		m_out += bracket;
		m_comma = false;
	}
	void close( char bracket ){
		m_out += bracket;
		m_comma = true; // whatever follows a closed container is a sibling
	}

public:
	const std::string& str() const { return m_out; }
	bool empty() const { return m_out.empty(); }

	Writer& beginObject(){
		open( '{' );
		return *this;
	}
	Writer& endObject(){
		close( '}' );
		return *this;
	}
	Writer& beginArray(){
		open( '[' );
		return *this;
	}
	Writer& endArray(){
		close( ']' );
		return *this;
	}

	/// \brief Writes an object member name. The next written value becomes its value.
	Writer& key( const char* name ){
		separate();
		string_raw( name );
		m_out += ':';
		m_comma = false;
		return *this;
	}

	Writer& null(){
		separate();
		m_out += "null";
		return *this;
	}
	Writer& boolean( bool value ){
		separate();
		m_out += value ? "true" : "false";
		return *this;
	}
	Writer& number( double value ){
		separate();
		if ( !std::isfinite( value ) ) {
			m_out += "null"; // JSON has no way to say this
			return *this;
		}
		char buffer[40];
		if ( value == std::floor( value ) && std::fabs( value ) < 1e15 ) {
			snprintf( buffer, sizeof( buffer ), "%.0f", value );
		}
		else {
			// shortest representation that reads back identically
			snprintf( buffer, sizeof( buffer ), "%.9g", value );
			if ( strtod( buffer, 0 ) != value ) {
				snprintf( buffer, sizeof( buffer ), "%.17g", value );
			}
		}
		m_out += buffer;
		return *this;
	}
	Writer& integer( std::size_t value ){
		return number( double( value ) );
	}
	Writer& string( const char* value ){
		separate();
		string_raw( value != 0 ? value : "" );
		return *this;
	}

	/// \brief Splices an already-serialised document in as one value.
	/// Used to assemble a response around a result that was built separately, so
	/// that a method which fails halfway cannot leave half a value behind.
	Writer& raw( const std::string& document ){
		separate();
		m_out += document.empty() ? "null" : document;
		return *this;
	}

	/// \brief Writes [x, y, z] from a float triple.
	Writer& vector3( const float* value ){
		beginArray();
		number( value[0] );
		number( value[1] );
		number( value[2] );
		return endArray();
	}

	/// \brief Writes a quoted, escaped string without separator handling.
	void string_raw( const char* value ){
		m_out += '"';
		for ( const char* c = value; *c != '\0'; ++c )
		{
			switch ( *c )
			{
			case '"': m_out += "\\\""; break;
			case '\\': m_out += "\\\\"; break;
			case '\b': m_out += "\\b"; break;
			case '\f': m_out += "\\f"; break;
			case '\n': m_out += "\\n"; break;
			case '\r': m_out += "\\r"; break;
			case '\t': m_out += "\\t"; break;
			default:
				if ( ( unsigned char )*c < 0x20 ) {
					char buffer[8];
					snprintf( buffer, sizeof( buffer ), "\\u%04x", ( unsigned char )*c );
					m_out += buffer;
				}
				else {
					m_out += *c; // already UTF-8, or the caller's problem
				}
			}
		}
		m_out += '"';
	}
};

} // namespace json
