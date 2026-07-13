import secrets

def generate_secure_password(length: int = 12) -> str:
    """Generate a secure random password.

    The password will include at least one digit, uppercase letter,
    lowercase letter, and symbol, with the remaining characters
    randomly selected from all these categories.

    Uses Python's ``secrets`` module (cryptographically strong PRNG) instead
    of ``random`` so that generated Entra ID passwords are unpredictable even
    if an attacker observes other outputs from the process.

    Args:
        length: The length of the password to generate (default: 12)

    Returns:
        A secure random password string
    """
    # declare arrays of the character that we need in out password
    DIGITS = ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9']
    LOCASE_CHARACTERS = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h',
                        'i', 'j', 'k', 'm', 'n', 'o', 'p', 'q',
                        'r', 's', 't', 'u', 'v', 'w', 'x', 'y',
                        'z']

    UPCASE_CHARACTERS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H',
                        'I', 'J', 'K', 'M', 'N', 'O', 'P', 'Q',
                        'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y',
                        'Z']

    SYMBOLS = ['@', '#', '$', '%', '=', ':', '?', '.', '/', '|', '~', '>',
              '*', '(', ')', '<']

    # combines all the character arrays above to form one array
    COMBINED_LIST = DIGITS + UPCASE_CHARACTERS + LOCASE_CHARACTERS + SYMBOLS

    # randomly select at least one character from each character set above
    rand_digit = secrets.choice(DIGITS)
    rand_upper = secrets.choice(UPCASE_CHARACTERS)
    rand_lower = secrets.choice(LOCASE_CHARACTERS)
    rand_symbol = secrets.choice(SYMBOLS)

    # combine the character randomly selected above
    # at this stage, the password contains only 4 characters but
    # we want a password of the specified length
    temp_pass = rand_digit + rand_upper + rand_lower + rand_symbol

    # now that we are sure we have at least one character from each
    # set of characters, we fill the rest of
    # the password length by selecting randomly from the combined
    # list of character above.
    for x in range(length - 4):
        temp_pass = temp_pass + secrets.choice(COMBINED_LIST)

    # convert temporary password into a list and shuffle to
    # prevent it from having a consistent pattern
    # where the beginning of the password is predictable.
    # Fisher-Yates with secrets.randbelow gives a CSPRNG-backed shuffle.
    temp_pass_list = list(temp_pass)
    for i in range(len(temp_pass_list) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        temp_pass_list[i], temp_pass_list[j] = temp_pass_list[j], temp_pass_list[i]

    return "".join(temp_pass_list)