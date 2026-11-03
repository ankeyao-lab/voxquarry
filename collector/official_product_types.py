"""Product-type evidence for the Ryobi, Milwaukee and Greenworks US catalogs.

Accessory rules precede machine rules. Unknown names remain unknown; the
mapping must never turn an unrecognized product into a generic OPE accessory.
"""
import re

PASSIVE_TYPES = {'Tool Accessory', 'Hand Tool', 'Tool Storage', 'Workwear', 'Safety Equipment', 'Fastener', 'Mounting/Organization', 'Consumable'}
EXTRA_TYPES = tuple(sorted(PASSIVE_TYPES | {'Saw','Nailer','Stapler','Sander','Grinder','Router','Planer','Rotary Tool','Multi Tool','Impact Driver','Powered Screwdriver','Powered Ratchet','Power Tool Combo Kit','Measuring/Inspection Tool','Caulking Gun','Glue Gun','Soldering Tool','Heat Gun','Drain Cleaner','Grease Gun','Powered Cutter','Paint Sprayer','Heated Workwear','Welding Equipment','Sewing Tool','Garage Door Opener','Sewage/Septic Tool','Polishers','Electric Bicycle','Robot Vacuum','Service Plan','Plumbing Tool','Camera/Tracker','Powered Torque Wrench'}))
CHANNELS = {'ryobi','milwaukee','greenworks'}

def classify(title, raw_category=None, description=""):
    t = re.sub(r'\s+', ' ', re.sub(r'[™®]', '', str(title or ''))).casefold().replace('‑','-')
    t = re.sub(r"\b(?:with|w/)\s+one-key\b", "one-key", t)
    t = re.sub(r"\([^)]*\)", " ", t)
    # Included components after "with" or "w/" cannot replace the main tool.
    has_platform = bool(re.search(r"\b(?:m12|m18|mx fuel|one\+|usb lithium|cordless|\d+\s*[- ]?\s*(?:v|volt)|corded)\b", t))
    main = re.split(r"\b(?:with|includes?|including)\b|\bw/|:(?=\s)|,(?=\s)", t, maxsplit=1)[0].strip() if has_platform or re.search(r"\b(?:saw|pressure washer|mower|blower|trimmer)\b", t) and not re.search(r"\b(?:blade|bit|sawzall.*carbide)\b", t) else t

    early = (
        ('Tool Accessory', r'\b(?:shockwave.*drill.*drive set|\d+t .*circular saw.*(?:pack|bulk)|blades?|bits?|sds[-+ ]+(?:plus |4ct |mx4)|impact driving kit|construction framing contractor|metal cut off wheel)\b'),
        ('Mounting/Organization',r'\bwall rails?\b'),
        ('Workwear',r'\b(?:tech pants|hybrid tee|long sleeve|short sleeve)\b'),
        ('Tool Storage',r'\b(?:steel storage|workstation|packout.*(?:crate|cart))\b'),
        ('Service Plan',r'\bprotection plan\b'),
        ('Electric Bicycle',r'\belectric.*bike\b'),
        ('Robot Vacuum',r'\brobot vacuum\b'),
    )
    for category, pattern in early:
        if re.search(pattern, main):
            # A circular saw marketed with a blade is still the powered saw.
            if category == 'Tool Accessory' and re.search(r'\b(?:diamond blade corded|wide blade magnetic tape|rotary hammer|multi.blade.*mower|dual blade.*mower|blade.head.*trimmer)\b', main):continue
            return category
    rules = (
        ('Heated Workwear', r'\bheated\b.*\b(?:jacket|hoodie|vest|shirt|gear|glove)\b'),
        ('Tool Accessory', r'\b(?:saw stand|router table|quickstand|drill bits?|bit set|bit kit|hole saw|hole dozer|big hawg|saw blades?|circular saw blade|sawzall blade|multi.tool blade|cut.off wheel|cut off wheel|grinding wheel|sanding (?:disc|belt|pad|sheet)|arbor|mandrel|bit holder|nut driver|driver bit|impact bit|sockets?|saw guide|saw track|replacement blade|spade bit|auger bit|forstner|countersink|step drill bit|diamond max hole)\b'),
        ('OPE Accessory', r'\b(?:bagging kit|chainsaw chain|replacement chain|saw chain|replacement spool|trimmer line|trimmer head|mower blade|mower cover|mulch plug|guide bar|replacement bar|edger blade|blower nozzle|replacement tine|trimmer replacement|replacement string)\b'),
        ('Pressure Washer Accessory', r'\b(?:surface cleaner|water broom|turbo nozzle|pressure washer (?:hose|accessor|nozzle|wand|trigger gun|gun)|foam cannon|foam blaster|quick.connect nozzle)\b'),
        ('Vacuum Accessory', r'\b(?:vacuum (?:filter|bag|hose|accessor|nozzle|attachment)|replacement filter|hepa filter)\b'),
        ('Fastener', r'\b(?:brad nails|finish nails|framing nails|staples|collated screws)\b'),
        ('Safety Equipment', r'\b(?:safety glasses|safety goggles|safety helmet|hard hat|ear plugs|earplugs|earmuff|respirator|dust mask|face shield|safety vest|safety lens|knee pads|kneepads)\b'),
        ('Workwear', r'\b(?:gloves?|hoodie|jacket|t.shirt|work shirt|workwear|beanie|knit hat|work pants|workskin|headlamp mount|sunshade|cooling towel|boots?|base layer|neck gaiter)\b'),
        ('Tool Storage', r'\b(?:tool box|toolbox|tool chest|tool bag|tool backpack|tool organizer|organizer|rolling cabinet|storage cabinet|storage case|storage tote|carry.all|carrying case|bucket organizer|packout.*(?:box|rack|shelf|cabinet|tote|bag|drawer|chest)|rolling (?:storage|tool)|link.*(?:storage|box|tool tote))\b'),
        ('Mounting/Organization', r'\b(?:wall rail|wall mount|mounting plate|storage hook|tool hook|magnetic holder|battery holder|tool holder|belt clip|belt hook|wall shelf)\b'),
        ('Cooler', r'\b(?:cooler|tumbler|drink bottle|water bottle|insulated bottle|travel mug)\b'),
        ('Hand Tool', r'\b(?:pliers?|hand wrench|adjustable wrench|combination wrench|ratcheting wrench|pipe wrench|torque wrench|wrench set|hammer|mallet|screwdriver|hex key|hex keys|utility knife|pocket knife|fastback|snips|scissors|bolt cutter|hand saw|hand (?:ratchet|tool)|ratchet and socket|ratchet & socket|pry bar|clamp|locking (?:plier|jaw)|wire stripper|cable stripper|tape measures?|ruler|square|spirit level|torpedo level)\b'),
        ('Measuring/Inspection Tool', r'\b(?:laser|thermometer|temp.gun|inspection (?:scope|camera)|multimeter|voltage detector|voltage tester|stud finder|stud detector|moisture meter|distance measur|digital level|clamp meter|test lead|circuit tester|inspection kit)\b'),
        ('Paint Sprayer', r'\b(?:paint sprayer|airless sprayer|hvlp|spray station)\b'),
        ('Nailer', r'\bnailer\b'), ('Stapler', r'\bstapler\b'),
        ('Soldering Tool', r'\bsolder(?:ing)?\b'), ('Glue Gun', r'\bglue gun\b'),
        ('Caulking Gun', r'\b(?:caulk|adhesive gun)\b'), ('Grease Gun', r'\bgrease gun\b'),
        ('Heat Gun', r'\bheat gun\b'), ('Drain Cleaner', r'\b(?:drain (?:cleaner|auger|snake)|air snake)\b'),
        ('Garage Door Opener', r'\bgarage door opener\b'),
        ('Sewing Tool', r'\b(?:stitching|sewing)\b'),
        ('Powered Cutter', r'\b(?:power cutter|bolt cutter|cable cutter|cut.out tool|cut out tool|shear|nibbler|pex cutter|tubing cutter|pipe cutter|press tool|crimping tool|rivet tool|rivet gun)\b'),
        ('Impact Driver', r'\b(?:impact driver|pulse driver|hydraulic driver)\b'), ('Powered Ratchet', r'\bratchet\b'),
        ('Polishers',r'\b(?:polisher|buffer)\b'), ('Camera/Tracker',r'\b(?:security camera|equipment tracker)\b'),
        ('Plumbing Tool',r'\b(?:pipe threader|propex.*expander|pulling fish tape)\b'),
        ('Powered Screwdriver',r'\bscrew gun\b'),
        ('Powered Cutter',r'\b(?:cut.off tool|power carver)\b'),
        ('Drain Cleaner',r'\bsectional drum machine\b'),
        ('Glue Gun',r'\bglue pen\b'),
        ('Multi Tool', r'\b(?:multi.tool|jobplus|multi.tool)\b'),
        ('Rotary Tool', r'\b(?:rotary tool|rotary cutter|speed saw)\b'),
        ('Sander', r'\b(?:sander|bandfile)\b'), ('Grinder', r'\bgrinder\b'), ('Router', r'\brouter\b'), ('Planer', r'\bplaner\b'),
        ('Saw', r'\b(?:circular saw|reciprocating saw|miter saw|mitre saw|jig saw|jigsaw|band saw|bandsaw|table saw|tile saw|flooring saw|plunge saw|scroll saw|track saw|cut.off saw|cut off saw|sawzall|hackzall|cut.off machine)\b'),
        ('Battery Pack', r'\b(?:batteries|battery|starter kit)\b'),
        ('Charger', r'\b(?:charger|supercharger|charge station)\b'),
        ('Light', r'\b(?:light|lighting|penlight|flashlight|spotlight|floodlight|lantern|headlamp|hi.beam)\b'),
        ('Radio/Speaker', r'\b(?:radio|speaker|stereo|ear buds|earbuds)\b'),
    )
    powered = bool(re.search(r'\b(?:m12|m18|mx fuel|one\+|usb lithium|cordless|\d+\s*[- ]?\s*v|\d+\s*[- ]?\s*volt)', main))
    # Kits with named distinct tools are not an included battery/charger kit.
    if re.search(r'\bchain\s?saw\b.*\band\s+\d+\s*v\b.*\bpruner\b', main):
        return 'OPE Combo Kit'
    if re.search(r'\bcombo\b|\b(?:\d+)[- ]?(?:tool|pc\.?|piece) (?:kit|super|ultimate)', main):
        if re.search(r'\b(?:mower|trimmer|blower|chainsaw|chain saw|edger|hedge)\b',main): return 'OPE Combo Kit'
        if not re.search(r'\b(?:battery|batteries|charger)\b',main): return 'Power Tool Combo Kit'
    # Whole OPE appliances must precede generic battery, light and hand-tool words.
    whole = (
        ('OPE Accessory',r'\b(?:bladed head|fixed line.*head|gutter cleaning kit)\b'),
        ('Power Station/Inverter',r'\b(?:power source|power station|power inverter|power supply)\b'),
        ('Heater',r'\bheater\b'), ('Mixer',r'\bmixer\b'), ('Bug Zapper',r'\bbug zapper\b'),
        ('Spot Cleaner',r'\b(?:spot cleaner|spot (?:and|&) carpet cleaner)\b'), ('Hard Floor Cleaner',r'\bspray mop\b'),
        ('Multi Head',r'\bpower head\b'), ('Auger',r'\b(?:earth auger|ice auger)\b'),
        ('Scarifier',r'\b(?:dethatcher|scarifier)\b'), ('Sprayer',r'\b(?:chemical fogger|fogger/mister|sprayer)\b'),
        ('Seeder',r'\bspreader\b'),
        ('Cultivator',r'\b(?:cultivator|tiller)\b'),
        ('Radio/Speaker',r'\b(?:clamp speakers?|verse.*speaker)\b'),
        ('Riding Lawnmower', r'\b(?:zero.turn|riding|stand.on|standon)\b.*\b(?:mower|lawn|z.trak)|\bmaximusz\b'),
        ('Robot Mower',r'\b(?:robotic|robot)\b.*\b(?:mower|lawn)\b'),
        ('Walk behind Lawnmower', r'\b(?:lawn ?mower|mower|mowing)\b'),
        ('String Trimmer', r'\b(?:string trimmer|grass trimmer|line trimmer|expand.it trimmer|curved shaft trimmer)\b'),
        ('Edger', r'\bedger\b'),
        ('Hedge Trimmer', r'\bhedge trimmer\b'), ('Pruner', r'\b(?:pole saw|pole chainsaw|lopping shear|pruner|lopper|pruning shears?|pruning saw)\b'),
        ('Chain Saw', r'\bchain\s?saw\b'), ('Leaf Blower/Vacuum',r'\b(?:leaf blower|blower|blower/vac|leaf vacuum|\d+ cfm sweeper)\b'),
        ('Snow Blower',r'\bsnow (?:blower|thrower)\b'), ('Snow Shovel',r'\bsnow shovel\b'),
        ('Pressure Washer',r'\bpressure washer\b'), ('Power Cleaner',r'\b(?:power cleaner|ezclean)\b'),
        ('Inflator',r'\b(?:inflator|deflator)\b'), ('Air Compressor',r'\bcompressor\b'),
        ('Fan',r'\bfan\b'), ('Wet/Dry Vacuum',r'\bwet\s?[/&-]\s?dry\b'),
        ('Cordless Handheld Vacuum',r'\b(?:hand vacuum|handheld vacuum|compact vacuum)\b'), ('Cordless Stick Vacuum',r'\bstick vacuum\b'),
        ('Power Scrubber',r'\b(?:power scrubber|spin scrubber|scrubber)\b'),
        ('Drill',r'\b(?:drill|rotary hammer)\b'), ('Impact Wrench',r'\b(?:impact wrench|htiw)\b'),
        ('Pump',r'\b(?:transfer pump|water pump|submersible pump)\b'),
    )
    # Explicit replacement/accessory wording always wins over whole-machine words.
    for category, pattern in rules[:11]:
        if re.search(pattern,main): return category
    if re.search(r'\b(?:attachment(?! capable)|replacement|accessory|accessories)\b',main):
        if re.search(r'\b(?:trimmer|mower|blower|chainsaw|edger|tiller|cultivator|auger|sweeper|pole saw|pruner|brush cutter|quik.lok|broom|bristle brush|snow thrower|snow blower)\b',main):return 'OPE Accessory'
        if re.search(r'\b(?:vacuum|vac)\b',main):return 'Vacuum Accessory'
    if re.search(r'\b(?:hand|manual)\b',main) and re.search(r'\b(?:pruner|shear|lopper)\b',main) and not powered:return 'Hand Tool'
    if re.search(r'\b(?:paint sprayer|hvlp|airless sprayer)\b',main):return 'Paint Sprayer'
    for category,pattern in whole:
        if re.search(pattern,main):return category
    for category,pattern in rules[11:]:
        if re.search(pattern,main):
            if category == 'Measuring/Inspection Tool' and re.search(r'\b(?:miter saw|mitre saw|circular saw|jig saw)\b',main):continue
            if category=='Hand Tool' and powered:
                if 'torque wrench' in main:return 'Powered Torque Wrench'
                if 'pinch clamp' in main:return 'Plumbing Tool'
                if 'screwdriver' in main:return 'Powered Screwdriver'
                if 'ratchet' in main:return 'Powered Ratchet'
                if re.search(r'clamp.*(?:fan|light)|(?:fan|light).*clamp',main):continue
                if re.search(r'\b(?:cutter|stripper|snips|scissors|shear)\b',main):return 'Powered Cutter'
                if 'hammer' in main:continue
            return category
    if "sweeper" in main and re.search(r"\b\d+\s*(?:mph|cfm)\b", description, re.I):
        return "Leaf Blower/Vacuum"
    return None
