import sys
path = 'C:/Users/harsh/UITEST-FRONTEND/src/components/Sidebar.jsx'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

lines = content.splitlines()
out_lines = []
for i, line in enumerate(lines):
    out_lines.append(line)
    if "<span>Executions</span>" in line:
        # The next line is </div> and then </div>
        # We want to insert the Lighthouse tab after the first </div>
        pass

# Actually simpler:
target1 = "<span>Executions</span>}"
target2 = "</div>"

new_content = []
i = 0
while i < len(lines):
    new_content.append(lines[i])
    if "<span>Executions</span>}" in lines[i]:
        # we found the executions span.
        # the next line should be </div>
        if i + 1 < len(lines) and "</div>" in lines[i+1]:
            new_content.append(lines[i+1])
            i += 1
            # Now append our new Lighthouse nav-item
            new_content.append('\t\t\t\t<div')
            new_content.append('\t\t\t\t\tclassName={
av-item }')
            new_content.append('\t\t\t\t\tonClick={() => onViewChange(VIEW_TYPES.LIGHTHOUSE)}')
            new_content.append('\t\t\t\t\ttitle="Lighthouse Performance"')
            new_content.append('\t\t\t\t>')
            new_content.append('\t\t\t\t\t<span className="nav-icon">??</span>')
            new_content.append('\t\t\t\t\t{!collapsed && <span>Lighthouse</span>}')
            new_content.append('\t\t\t\t</div>')
    i += 1

with open(path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(new_content))
print("Replaced successfully")
