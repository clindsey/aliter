import re
import thread

from app import log

from app.constants import MAIN_THREAD

from struct import pack, unpack, calcsize
from twisted.internet import reactor

from misc import expandStruct

from exceptions import MissingArgument


# Custom struct codes:
# ! = Variable length string (Only works with words and must be first argument)
# Dict format: "packetNum: (method, struct[, arguments]*)"
receivedPackets = {
    #------------------------------------------------------
    # Login server packets
    
    'login': {
        0x064: ['login', 'l24s24sB', 'packetVersion', 'username', 'password', 'region'],
    },
    
    #------------------------------------------------------
    # Char server packets
    
    'char': {
        0x065: ['identify', 'lLLxxB', 'accountID', 'loginIDa', 'loginIDb', 'sex'],
        0x066: ['selectChar', 'b', 'charNum'],
        0x067: ['createChar', '24s8BxBx', 'name', 'str', 'agi', 'vit', 'int', 'dex', 'luk', 'charNum', 'hairStyle', 'hairColor'],
        0x068: ['deleteChar', 'l40s', 'characterID', 'Email'],
        0x187: ['keepAlive', 'l', 'kaAccountID'],
    },
    
    #------------------------------------------------------
    # Map server packets
    
    'map': {
        0x07d: [], # Map loading complete
        0x085: [None, '10x'], # Gets sent when talking to NPCs
        0x089: ['keepAlive', 'xxl', 'clientTick'],
        0x08c: ['getActorName', 'xxxxxl', 'actorID'], # Hover over actor
        0x090: ['npcActivate', 'lx', 'npcID'], # Activate NPC
        0x099: ['announce', 'h!', 'packetLen', 'message'], # /b Message
        0x09b: ['identify', 'xxlxlxxxxLxxxxx', 'accountID', 'characterID', 'loginIDa'],
        0x0a2: ['characterName', '9xl', 'characterID'], # Request character name of forged item
        0x0a7: ['move', 'xxx3s', 'position'], # Character movement
        0x0b2: ['menuButton', 'B', 'type'], # Character select / Last save point menu button
        0x0b8: ['npcMenuSelect', 'lB', 'accountID', 'selection'], # Selected item from NPC menu (First=1)
        0x0b9: ['npcNext', 'l', 'accountID'], # Clicked the NPC next button
        0x0bf: ['emotion', 'B', 'emotion'], # Character emotion
        0x0f3: ['speech', 'h!', 'packetLen', 'message'], # Talking
        0x143: ['npcNumInput', 'l', 'accountID'], # NPC numerical input
        0x146: ['npcClosed', 'l', 'accountID'], # NPC close button was pressed
        0x14d: ['guildPage'], # Request guild page
        0x14f: ['guildInfo', 'l', 'page'], # Request guild information tab
        0x151: ['guildEmblem', 'l', 'guildID'], # Request guild emblem
        0x159: ['guildLeave', 'lll40s', 'guildID', 'accountID', 'characterID', 'message'], # Leaving guild
        0x15b: ['guildExpel', 'lll40s', 'guildID', 'accountID', 'characterID', 'message'], # Expel from guild
        0x165: ['guildCreate', 'l24s', 'accountID', 'guildName'], # Create guild
        0x168: ['guildInvite', 'lll', 'accountID', 'inviterAccountID', 'characterID'], # Invite
        0x16b: ['guildReject', 'll', 'guildID', 'type'], # Guild request response (type: 1 = accept, 0 = reject)
        0x16e: ['guildSetAnnouncement', 'l60s120s', 'guildID', 'title', 'body'], # Set guild announcement
        0x170: ['guildAllianceRequest', 'lll', 'accountID', 'sourceAccountID', 'characterID'], # Send guild alliance request (FIXME: The names of these arguments need to be verified.)
        0x172: ['guildAllianceRespond', 'll', 'accountID', 'type'], # Respond to guild alliance request (type: 1 = accept, 0 = rejext)
        0x18a: ['quit', 'xx'],
        0x1d5: ['npcStrInput', 'wl!', 'packetLen', 'accountID', 'message'], # NPC string input (NUL terminated)
        0x21d: [None, 'l', 'disabled'], # Does user have /effect disabled?
    },
}

# Custom struct codes:
# ? = Subpacket
# ! = Variable length string
# Dict format: "packetNum: (struct, (arguments)[, subpackets])"
sentPackets = {
    #------------------------------------------------------
    # Subpackets
    
    'charServer': ('4sh20shhh', ('ip', 'port', 'name', 0, 'maintenance', 'new')),
    'character':  (
        '5l8s3l17h24s6Bhh',
        (
            'id', 'baseExp', 'zeny', 'jobExp', 'jobLevel', '', 0, 0, 0, 'statusPoints', 'hp', 'maxHP', 'sp', 'maxSP', 'speed', 'job', 'hairStyle',
            'viewWeapon', 'baseLevel', 'skillPoints', 'viewHeadBottom', 'viewShield', 'viewHeadTop', 'viewHeadMiddle', 'hairColor', 'clothesColor',
            'name', 'str', 'agi', 'vit', 'int', 'dex', 'luk', 'charNum', 1
        )
    ),
    "inventoryItem": ('2h2BH2x4H', ('index', 'itemID', 'type', 'identified', 'amount', 'card1', 'card2', 'card3', 'card4')),
    "inventoryEquip": ('hhBBhhxB4h', ('index', 'itemID', 'type', 'identified', 'equipLocations', 'equipPoint', 'refine', 'card1', 'card2', 'card3', 'card4')),
    
    #------------------------------------------------------
    # Special versions of packets
    
    'ack':      ('l', ('accountID',)), # Acknowledge login/char server connection (0283 prefixed)
    'map':      ('hl', (0x0187, 'accountID',)), # Acknowledge map server connection
    'viewNPC':  ('hxlh6xh30x3s3xB', (0x78, 'actorID', 200, 'sprite', 'position', 1)), # Display NPC on map
    'viewWarp': ('hxlh6xh30x3s3xB', (0x78, 'actorID', 200, 45, 'position', 1)), # Display warp on map
    
    #------------------------------------------------------
    # Login server packets
    
    0x069: ('hLlLl24shB?', ('packetLen', 'loginIDa', 'accountID', 'loginIDb', 0, '', 0, 'gender'), ('charServer',)), # Logged in / Char server listing
    0x06a: ('B20s', ('type', 'message')), # Login failed
    
    #------------------------------------------------------
    # Char server packets
    
    0x06b: ('h20s?', ('packetLen', ''), ('character',)), # List characters
    0x06c: ('h', ('type',)), # Character selection failed/character server error
    0x06d: ('?', None, ('character',)), # Create character
    0x06e: ('h', ('type',)), # Character creation failed
    0x06f: None, # Character deleted
    0x070: ('h', ('type',)), # Character deletion failed
    0x071: ('l16s4sh', ('characterID', 'map', 'ip', 'port')), # Character selected
    
    #------------------------------------------------------
    # Map server packets
    
    0x073: ('l3sxx', ('tick', 'position')), # Login successful
    0x07f: ('l', ('tick',)), # Sync [Alex]
    0x080: ('lb', ('actorID', 'style')), # Actor vanished (0=off-screen, 1=died, 2=vanished, 3=teleport)
    0x081: ('b', ('type',)), # Error (0=server shut down, 1=server closed, 2=dual login prohibited, 3=out of sync, 4=server is jammed, 5=underaged, 6=pay to play, 8=server still recognizes you as connected, 9=internet cafe full, 10=payment expired, 15=kicked)
    0x086: ('l6sl', ('actorID', 'position', 'tick')), # Actor display
    0x087: ('l6sl', ('actorID', 'position', 'tick')), # Actor movement
    0x08a: ('lllllhhBh', ('src', 'dst', 'tick', 'srcSpeed', 'dstSpeed', 'paramA', 'paramB', 'type', 'paramC')), # Physical attack
    0x08d: ('hl!', ('packetLen', 'actorID', 'message')), # Normal chat message with ID
    0x08e: ('h!', ('packetLen', 'message')), # Normal chat message
    0x091: ('16shh', ('map', 'x', 'y')), # Change map on same server
    0x095: ('l24s', ('actorID', 'name')), # Display actor name
    0x09a: ('h!', ('packetLen', 'message')), # /b Message
    0x0a0: ('3h3B5h2B', ('index', 'amount', 'itemID', 'identified', 'broken', 'refine', 'card1', 'card2', 'card3', 'card4', 'equipLocations', 'type', 'fail')), # Item receiving
    0x0a4: ('h!', ('packetLen', 'data')), # Equipment (inventory)
    0x0b0: ('hl', ('type', 'value')), # Update character stats (type: 0 = speed (* 1000), 3 = ?, 4 = mute (seconds), 5 = HP, 6 = Max HP, 7 = SP, 8 = Max SP, 9 = status points, 11 = base level, 12 = skill points, 24 = weight, 25 = max weight, 41 = attack, 42 = attack bonus, 43 = matk max, 44 = matk min, 45 = defense, 46 = defense bonus, 47 = mdef, 48 = mdef bonus, 49 = hit, 50 = flee, 51 = flee bonus, 52 = critical, 53 = aspd, 55 = job level, 124 = ?)
    0x0b1: ('hl', ('type', 'value')), # Update other char stats (type: 1 = bexp, 2 = jexp, 20 = zeny, 22 = bexp required, 23 = jexp required)
    0x0b3: ('l', ('type',)), # Returned to character select screen
    0x0b4: ('hl!', ('packetLen', 'actorID', 'message')), # NPC message
    0x0b5: ('l', ('actorID',)), # NPC next button
    0x0b6: ('l', ('actorID',)), # NPC close button
    0x0b7: ('hl!', ('packetLen', 'actorID', 'items')), # NPC menu (Items seperated by ":")
    0x0bd: ('h12B11h4x', ('statusPoint', 'str', 'bstr', 'agi', 'bagi', 'vit', 'bvit', 'int', 'bint', 'dex', 'bdex', 'luk', 'bluk', 'atk', 'batk', 'matkmax', 'matkmin', 'def', 'bdef', 'mdef', 'bmdef', 'hit', 'flee', 'bflee', 'critical')), # Update stats
    0x0c0: ('lB', ('actorID', 'emotion')), # Display emotion with ID
    0x0cd: ('B', (0x81,)), # Player kicked.
    0x10f: ('h!', ('packetLen', 'data')), # Skill info (Data: <skill ID>.w <target type>.w ?.w <lv>.w <sp>.w <range>.w <skill name>.24B <up>.B)
    0x13a: ('h', ('range',)), # Attack range
    0x141: ('lll', ('type', 'base', 'bonus')), # Update a single stat (type: 13-18 = str, agi, vit, int, dex, luk)
    0x142: ('l', ('actorID',)), # NPC numerical input
    0x144: ('4l4Bx', ('actorID', 'type', 'x', 'y', 'pointID', 'red', 'green', 'blue')), # Mark the minimap (Type 2 = Remove)
    0x14e: ('l', ('type',)), # Guild page response (87 or 0x57 = member, 215 or 0xd7 = guild master)
    0x150: ('11l24s24s16s', ('guildID', 'level', 0, 'capacity', 0, 'exp', 'nextExp', 'tax', 0, 0, 'members', 'name', 'master', '')), # Guild information response
    0x152: ('hll!', ('packetLen', 'guildID', 'emblemID', 'emblem')), # Send guild emblem
    0x154: ('h!', ('packetLen', 'info')), # Info: {<accID>.l <charactorID>.l <?>.w <?.w <?>.w <job>.w <lvl?>.w <?>.l <online>.l <Position>.l ?.50B <nick>.24B} [repeated for each character]
    0x15a: ('24s40s', ('charName', 'message')), # Person left the guild
    0x15c: ('24s40s24x', ('charName', 'message')), # Person expelled from guild
    0x167: ('b', ('status',)), # Guild creation result (0 = success, 2 = name taken, 1 and 2 are probably "no emperium" and "invalid name", but I haven't checked (FIXME))
    0x169: ('b', ('type',)), # Guild invitation denied
    0x16a: ('l24s', ('guildID', 'name')), # Invited to guild (confirm dialogue)
    0x16c: ('l24s', ('guildID', 'name')), # Guild information
    0x16d: ('lll', ('guildID', 'characterID', 'online')), # Guild member has signed in
    0x16f: ('60s120s', ('title', 'body')), # Guild announcement
    0x171: ('l24s', ('sourceAccountID', 'name')), # Guild alliance invite
    0x173: ('b', ('type',)), # Alliance invite response
    0x17f: ('h!', ('packetLen', 'message')), # Guild chat message
    0x18b: ('l', ('failure',)), # Quit response
    0x194: ('l24s', ('actorID', 'name')), # Response to name request, e.g. forger names
    0x1b3: ('64sB', ('filename', 'position')), # NPC cut-in image
    0x1d4: ('l', ('actorID',)), # NPC string input
    0x1d7: ('lbhh', ('accountID', 'equip', 'w1', 'w2')), # Equip view grabbing
    0x1ee: ('h!', ('packetLen', 'data')), # Inventory (data: 'index', 'itemID', 'type', 'identified', 'broken', 'card1', 'card2', 'card3', 'card4')
    0x1f3: ('lh2x', ('accountID', 'effect')), # Effects
    # This beast handles a few times when a user should show up for other people. [Alex]
    0x22b: ('l4h2x10hl3h2x2b5sh', ('accountID', 'speed', 'opt1', 'opt2', 'opt3', 'job', 'hstyle', 'weapon', 'shield', 'lowhead', 'tophead', 'midhead', 'hcolor', 'ccolor', 'headdir', 'guildID', 'guildEmblem', 'manner', 'effect', 'karma', 'sex', 'position', 'blevel')),
    0x22c: ('xl4h2x5hL5hl3h2x2b8sh', ('accountID', 'speed', 'opt1', 'opt2', 'opt3', 'job', 'hstyle', 'weapon', 'shield', 'lowhead', 'tick', 'tophead', 'midhead', 'hcolor', 'ccolor', 'headdir', 'guildID', 'guildEmblem', 'manner', 'effect', 'karma', 'sex', 'position', 'blevel')),
}

def generatePacket(packetID, **kwargs):
    if packetID not in sentPackets:
        return False
    
    schema = sentPackets[packetID]
    
    if not schema: # Empty packet
        return pack('=h', packetID)
    
    offset       = -1
    packetLayout = schema[0]
    packetLenOffset = None
    arguments    = []
    
    # Append normal arguments
    if schema[1]:
        for argument in schema[1]:
            offset += 1
            if argument not in kwargs:
                if type(argument) == int or argument == '': # Blank arguments
                    arguments.append(argument)
                elif argument == 'packetLen':
                    packetLenOffset = offset
                    arguments.append(0)
                else:
                    log.console("Missing argument for packet %s: %s" % (packetID, argument), log.CRITICAL)
                
                continue
            
            arguments.append(kwargs[argument])
    
    # Append "?" subpackets
    # TODO: Recode similar to "!" variable length strings
    if len(schema) > 2:
        decoded    = re.sub('\d|x', '', expandStruct(packetLayout))
        index      = decoded.find('?')
        subPacket  = 0
        subPackets = []
        while index > -1:
            if schema[2][subPacket] not in kwargs:
                raise MissingArgument
            
            subPacketArgsList = kwargs[schema[2][subPacket]]
            subPacketData     = []
            
            for subPacketArgs in subPacketArgsList:
                subPacketData.append(generatePacket(schema[2][subPacket], **subPacketArgs))
            
            subPacketData = ''.join(subPacketData)
            subPackets.append(len(subPacketData))
            arguments.insert(index, subPacketData)
            subPacket += 1
            decoded    = decoded.replace('?', 'x', 1)
            index      = decoded.find('?')
        
        # Replace "?" with "Xs"
        index = packetLayout.rfind('?')
        while index > -1:
            length = subPackets.pop()
            packetLayout = packetLayout.rsplit('?', 1)
            packetLayout = packetLayout[0] + '%ss' % length + packetLayout[1]
            index  = packetLayout.rfind('?')
    
    # Convert "!" variable length strings
    # Warning: If one is after a "?" then things will screw up
    index = packetLayout.rfind('!')
    while index > -1:
        length = len(arguments[index])
        packetLayout = packetLayout.rsplit('!', 1)
        packetLayout = packetLayout[0] + '%ss' % length + packetLayout[1]
        index  = packetLayout.rfind('!')
    
    # Generate packet
    if type(packetID) == int:
        packetLayout = '=h'+packetLayout
        arguments.insert(0, packetID)
    else:
        packetLayout = '='+packetLayout
    
    if type(packetLenOffset) == int:
        # Warning: If this is after a "?" then things will screw up
        if type(packetID) == int:
            packetLenOffset += 1
        
        arguments[packetLenOffset] = calcsize(packetLayout)
    
    # log.console("Packing %s as %s! Args: %s" % (packetID, packetLayout, kwargs), log.HIGH)
    
    return pack(packetLayout, *arguments)

def sendPacket(function, packetID, **kwargs):
    packet = generatePacket(packetID, **kwargs)
    
    if packet:
        if MAIN_THREAD != thread.get_ident(): # Are we in a thread?
            reactor.callFromThread(function, packet)
        else:
            function(packet)
        
        return True
    
    log.console("Sending packet %d failed!" % int(packetID), log.HIGH)
    
    return False

def encodePosition(x, y, dir=0):
    posA=(x >> 2) & 0xff
    posB=((x << 6) | ((y >> 4) & 0x3f)) & 0xff
    posC=((y << 4) | (dir & 0x0f)) & 0xff
    return chr(posA) + chr(posB) + chr(posC)

def encodePositionMove(x, y, toX, toY):
    posA=(x >> 2) & 0xff
    posB=((x << 6) | ((y >> 4) & 0x3f)) & 0xff
    posC=((y << 4) | ((toX >> 6) & 0x0f)) & 0xff
    posD=((toX << 2) | ((toY >> 8) & 0x03)) & 0xff
    posE=toY & 0xff
    posF=((8 << 4) | (8 & 0x0f)) & 0xff
    return chr(posA) + chr(posB) + chr(posC) + chr(posD) + chr(posE) + chr(posF)

def decodePosition(position):
    a = ord(position[0])
    b = ord(position[1])
    c = ord(position[2])
    x = (a << 2) | ((b & 0xc0) >> 6)
    y = (((b & 0x3f) << 4) | ((c & 0xf0) >> 4))
    #dir = ord(position[2]) & 0x0f
    return x, y#, dir
