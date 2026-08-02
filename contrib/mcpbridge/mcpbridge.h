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
/// \brief A newline-delimited JSON-RPC 2.0 view of live editor state.
///
/// Off unless it is built with MCPBRIDGE_ENABLED *and* switched on at runtime.
/// Binds to 127.0.0.1 only. See README.md for the security note - anything
/// that can open a loopback socket can drive the editor.

#pragma once

#include <cstddef>

namespace MCPBridge
{

/// Reported by scene.stats so a client can refuse an interface it does not know.
const int c_protocolVersion = 1;

/// \brief Runtime settings. Persisted through the preference system, except for
/// the shared secret, which comes from the environment so that it never lands
/// in a settings file.
struct Settings
{
	bool enabled = false;       ///< "MCPBridge_Enabled": required for the socket to open
	int port = 27700;           ///< "MCPBridge_Port"
	int maxConnections = 4;     ///< "MCPBridge_MaxConnections"
	bool logCalls = true;       ///< "MCPBridge_LogCalls": per-method usage, for surface pruning
};

Settings& settings();

/// \brief Opens the listening socket. No-op when already listening.
/// \return false when disabled at build time, disabled by preference, or the
/// socket could not be bound; the reason is written to the error stream.
bool start();

/// \brief Closes the listening socket and every open connection.
void stop();

bool listening();

/// \brief Writes a per-method call count to the output stream.
/// Feeds the "cut every method with zero real-session usage" rule.
void reportUsage();

// _QERPluginTable entry points
const char* init( void* hApp, void* pMainWidget );
const char* getName();
const char* getCommandList();
const char* getCommandTitleList();
void dispatch( const char* command, float* vMin, float* vMax, bool bSingleBrush );

} // namespace MCPBridge
