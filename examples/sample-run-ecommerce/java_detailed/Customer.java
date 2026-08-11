import java.util.ArrayList;
import java.util.List;

public class Customer {
    private List<ShoppingCart> shoppingcarts;
    private String email;
    private String password;

    public Customer(List<ShoppingCart> shoppingcarts, String email, String password) {
        this.shoppingcarts = shoppingcarts;
        this.email = email;
        this.password = password;
    }

    public List<ShoppingCart> getShoppingcarts() {
        return this.shoppingcarts;
    }
    public void setShoppingcarts(List<ShoppingCart> shoppingcarts) {
        this.shoppingcarts = shoppingcarts;
    }
    public String getEmail() {
        return this.email;
    }
    public void setEmail(String email) {
        this.email = email;
    }
    public String getPassword() {
        return this.password;
    }
    public void setPassword(String password) {
        this.password = password;
    }

    public boolean login(String inputPassword) {
        return this.password.equals(inputPassword);
    }
}